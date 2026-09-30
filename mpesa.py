import base64
import datetime

import requests

import config
from db import get_db


def get_access_token():
    resp = requests.get(
        f"{config.MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
        auth=(config.MPESA_CONSUMER_KEY, config.MPESA_CONSUMER_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _password_and_timestamp():
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{config.MPESA_SHORTCODE}{config.MPESA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def initiate_stk_push(order_id: int, phone: str, amount: float) -> dict:
    """
    Fire an STK push so the customer pays straight into the ADMIN's
    paybill/till (PartyB = config.MPESA_SHORTCODE). Records a pending
    row in `transactions` so the callback can match it up later.
    """
    try:
        token = get_access_token()
    except requests.RequestException as e:
        return {"ok": False, "error": f"Could not reach Safaricom: {e}"}

    password, timestamp = _password_and_timestamp()

    payload = {
        "BusinessShortCode": config.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(round(amount)),
        "PartyA": phone,
        "PartyB": config.MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": config.MPESA_CALLBACK_URL,
        "AccountReference": f"ORDER{order_id}",
        "TransactionDesc": f"Payment for order #{order_id}",
    }

    resp = requests.post(
        f"{config.MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    result = resp.json()
    checkout_request_id = result.get("CheckoutRequestID")
    merchant_request_id = result.get("MerchantRequestID")

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO transactions
                   (order_id, phone, amount, checkout_request_id, merchant_request_id, status)
               VALUES (%s, %s, %s, %s, %s, 'initiated')""",
            (order_id, phone, amount, checkout_request_id, merchant_request_id),
        )
    conn.commit()

    return {"ok": resp.status_code == 200, "data": result}


def process_callback(payload: dict) -> None:
    """
    Handle Safaricom's asynchronous STK callback: mark the transaction
    success/failed, and if successful, split the order total into
    broker commission / platform fee / seller payout and queue payouts.
    """
    stk = payload.get("Body", {}).get("stkCallback", {})
    checkout_request_id = stk.get("CheckoutRequestID")
    result_code = stk.get("ResultCode")
    result_desc = stk.get("ResultDesc")

    mpesa_receipt = None
    if result_code == 0:
        for item in stk.get("CallbackMetadata", {}).get("Item", []):
            if item.get("Name") == "MpesaReceiptNumber":
                mpesa_receipt = item.get("Value")

    status = "success" if result_code == 0 else "failed"

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE transactions
                  SET status = %s, result_desc = %s, mpesa_receipt = %s, updated_at = NOW()
                WHERE checkout_request_id = %s
                RETURNING order_id""",
            (status, result_desc, mpesa_receipt, checkout_request_id),
        )
        row = cur.fetchone()

        if row and status == "success":
            order_id = row["order_id"]

            cur.execute("SELECT total_amount, broker_id, seller_id FROM orders WHERE id = %s", (order_id,))
            order = cur.fetchone()

            cur.execute("SELECT value FROM settings WHERE key = 'platform_fee_pct'")
            platform_fee_pct = float(cur.fetchone()["value"])
            cur.execute("SELECT value FROM settings WHERE key = 'default_broker_commission_pct'")
            commission_pct = float(cur.fetchone()["value"])

            if order["broker_id"]:
                cur.execute(
                    "SELECT commission_rate FROM broker_profiles WHERE user_id = %s",
                    (order["broker_id"],),
                )
                bp = cur.fetchone()
                if bp:
                    commission_pct = float(bp["commission_rate"])

            total = float(order["total_amount"])
            commission_amount = round(total * commission_pct / 100, 2) if order["broker_id"] else 0
            platform_fee = round(total * platform_fee_pct / 100, 2)
            seller_payout = round(total - commission_amount - platform_fee, 2)

            cur.execute(
                """UPDATE orders
                      SET status = 'paid', commission_amount = %s,
                          platform_fee = %s, seller_payout_amount = %s
                    WHERE id = %s""",
                (commission_amount, platform_fee, seller_payout, order_id),
            )

            if order["broker_id"] and commission_amount > 0:
                cur.execute(
                    "INSERT INTO payouts (recipient_id, order_id, amount) VALUES (%s, %s, %s)",
                    (order["broker_id"], order_id, commission_amount),
                )
            cur.execute(
                "INSERT INTO payouts (recipient_id, order_id, amount) VALUES (%s, %s, %s)",
                (order["seller_id"], order_id, seller_payout),
            )
    conn.commit()
