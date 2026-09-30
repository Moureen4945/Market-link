from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort

import config
from db import close_db, query, execute, get_db
from auth import (
    login_required, role_required, current_user, login_user,
    register_user, logout_user, dashboard_url_for, PHONE_RE,
)
import mpesa

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.teardown_appcontext(close_db)


@app.context_processor
def inject_globals():
    return {"app_name": config.APP_NAME}


# ---------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------
@app.route("/")
def index():
    if current_user():
        return redirect(url_for(dashboard_url_for(current_user()["role"])))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        ok, message = register_user(
            request.form["full_name"].strip(),
            request.form["email"].strip(),
            request.form["phone"].strip(),
            request.form["password"],
            request.form.get("role", "customer"),
        )
        flash(message, "success" if ok else "error")
        if ok:
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = login_user(request.form["email"].strip(), request.form["password"])
        if user:
            return redirect(url_for(dashboard_url_for(user["role"])))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# Customer dashboard
# ---------------------------------------------------------------------
@app.route("/customer/dashboard")
@role_required("customer")
def customer_dashboard():
    user = current_user()
    products = query(
        """SELECT p.*, sp.company_name FROM products p
           JOIN seller_profiles sp ON sp.user_id = p.seller_id
          WHERE p.is_active = TRUE AND sp.approved = TRUE AND p.stock > 0
          ORDER BY p.created_at DESC LIMIT 50"""
    )
    orders = query(
        """SELECT o.*, sp.company_name FROM orders o
           JOIN seller_profiles sp ON sp.user_id = o.seller_id
          WHERE o.customer_id = %(cid)s ORDER BY o.created_at DESC""",
        {"cid": user["id"]},
    )
    return render_template("customer_dashboard.html", user=user, products=products, orders=orders)


@app.route("/customer/checkout/<int:product_id>", methods=["GET", "POST"])
@role_required("customer")
def checkout(product_id):
    user = current_user()
    product = query("SELECT * FROM products WHERE id = %(id)s AND is_active = TRUE",
                     {"id": product_id}, fetch="one")
    if not product:
        abort(404)

    stk_result = None
    order_id = None
    order_total = None

    if request.method == "POST":
        quantity = max(1, int(request.form.get("quantity", 1)))
        phone = request.form.get("phone", "").strip()
        broker_id_raw = request.form.get("broker_id", "").strip()
        broker_id = int(broker_id_raw) if broker_id_raw else None

        if not PHONE_RE.match(phone):
            flash("Enter a valid M-Pesa phone number, format 2547XXXXXXXX.", "error")
        elif quantity > product["stock"]:
            flash("Not enough stock available.", "error")
        else:
            total = float(product["price"]) * quantity
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO orders (customer_id, seller_id, broker_id, total_amount, status)
                       VALUES (%s, %s, %s, %s, 'pending_payment') RETURNING id""",
                    (user["id"], product["seller_id"], broker_id, total),
                )
                order_id = cur.fetchone()["id"]
                cur.execute(
                    """INSERT INTO order_items (order_id, product_id, quantity, unit_price)
                       VALUES (%s, %s, %s, %s)""",
                    (order_id, product["id"], quantity, product["price"]),
                )
            conn.commit()

            result = mpesa.initiate_stk_push(order_id, phone, total)
            if result["ok"]:
                stk_result = result["data"]
                order_total = total
            else:
                flash("Could not start M-Pesa payment: " + str(result.get("error")), "error")

    return render_template(
        "checkout.html", product=product, stk_result=stk_result,
        order_id=order_id, order_total=order_total,
    )


# ---------------------------------------------------------------------
# Broker / manager dashboard
# ---------------------------------------------------------------------
@app.route("/broker/dashboard", methods=["GET", "POST"])
@role_required("broker")
def broker_dashboard():
    user = current_user()
    profile = query("SELECT * FROM broker_profiles WHERE user_id = %(id)s", {"id": user["id"]}, fetch="one")

    if request.method == "POST" and "create_link" in request.form:
        if not profile["approved"]:
            flash("Your broker account is not yet approved by admin.", "error")
        else:
            execute(
                """INSERT INTO broker_links (broker_id, customer_id, seller_id, note)
                   VALUES (%(b)s, %(c)s, %(s)s, %(n)s)""",
                {
                    "b": user["id"],
                    "c": int(request.form["customer_id"]),
                    "s": int(request.form["seller_id"]),
                    "n": request.form.get("note", "").strip(),
                },
            )
            flash(f"Link created. Share your Broker ID ({user['id']}) with the customer so it's credited on checkout.", "success")
            return redirect(url_for("broker_dashboard"))

    orders = query(
        """SELECT o.*, u.full_name AS customer_name, sp.company_name FROM orders o
           JOIN users u ON u.id = o.customer_id
           JOIN seller_profiles sp ON sp.user_id = o.seller_id
          WHERE o.broker_id = %(bid)s ORDER BY o.created_at DESC""",
        {"bid": user["id"]},
    )
    total_earned = sum(float(o["commission_amount"]) for o in orders)

    links = query(
        """SELECT bl.*, u.full_name AS customer_name, sp.company_name FROM broker_links bl
           JOIN users u ON u.id = bl.customer_id
           JOIN seller_profiles sp ON sp.user_id = bl.seller_id
          WHERE bl.broker_id = %(bid)s ORDER BY bl.created_at DESC""",
        {"bid": user["id"]},
    )
    customers = query("SELECT id, full_name, email FROM users WHERE role = 'customer' ORDER BY full_name")
    sellers = query(
        """SELECT u.id, sp.company_name FROM users u JOIN seller_profiles sp ON sp.user_id = u.id
           WHERE u.role = 'seller' AND sp.approved = TRUE ORDER BY sp.company_name"""
    )

    return render_template(
        "broker_dashboard.html", user=user, profile=profile, orders=orders,
        total_earned=total_earned, links=links, customers=customers, sellers=sellers,
    )


# ---------------------------------------------------------------------
# Seller / company dashboard
# ---------------------------------------------------------------------
@app.route("/seller/dashboard", methods=["GET", "POST"])
@role_required("seller")
def seller_dashboard():
    user = current_user()
    profile = query("SELECT * FROM seller_profiles WHERE user_id = %(id)s", {"id": user["id"]}, fetch="one")

    if request.method == "POST" and "add_product" in request.form:
        if not profile["approved"]:
            flash("Your seller account is pending admin approval before you can list products.", "error")
        else:
            execute(
                """INSERT INTO products (seller_id, name, description, price, stock)
                   VALUES (%(sid)s, %(name)s, %(desc)s, %(price)s, %(stock)s)""",
                {
                    "sid": user["id"],
                    "name": request.form["name"].strip(),
                    "desc": request.form.get("description", "").strip(),
                    "price": float(request.form["price"]),
                    "stock": int(request.form["stock"]),
                },
            )
            flash("Product added.", "success")
            return redirect(url_for("seller_dashboard"))

    products = query("SELECT * FROM products WHERE seller_id = %(sid)s ORDER BY created_at DESC", {"sid": user["id"]})
    orders = query(
        """SELECT o.*, u.full_name AS customer_name FROM orders o
           JOIN users u ON u.id = o.customer_id
          WHERE o.seller_id = %(sid)s ORDER BY o.created_at DESC""",
        {"sid": user["id"]},
    )
    total_sales = sum(float(o["total_amount"]) for o in orders if o["status"] != "pending_payment")
    total_payout = sum(float(o["seller_payout_amount"]) for o in orders if o["status"] != "pending_payment")

    return render_template(
        "seller_dashboard.html", user=user, profile=profile, products=products,
        orders=orders, total_sales=total_sales, total_payout=total_payout,
    )


# ---------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------
@app.route("/admin/dashboard", methods=["GET", "POST"])
@role_required("admin")
def admin_dashboard():
    if request.method == "POST":
        if "approve_seller" in request.form:
            execute("UPDATE seller_profiles SET approved = TRUE WHERE user_id = %(id)s",
                    {"id": int(request.form["user_id"])})
            flash("Seller approved.", "success")
        elif "approve_broker" in request.form:
            execute("UPDATE broker_profiles SET approved = TRUE WHERE user_id = %(id)s",
                    {"id": int(request.form["user_id"])})
            flash("Broker approved.", "success")
        elif "set_commission" in request.form:
            execute("UPDATE broker_profiles SET commission_rate = %(rate)s WHERE user_id = %(id)s",
                    {"rate": float(request.form["rate"]), "id": int(request.form["user_id"])})
            flash("Commission rate updated.", "success")
        elif "mark_paid" in request.form:
            execute("UPDATE payouts SET status = 'paid', paid_at = NOW() WHERE id = %(id)s",
                    {"id": int(request.form["payout_id"])})
            flash("Payout marked as paid. Send the actual funds via M-Pesa B2C / bank transfer separately.", "success")
        elif "update_settings" in request.form:
            execute("UPDATE settings SET value = %(v)s WHERE key = 'platform_fee_pct'",
                    {"v": float(request.form["platform_fee_pct"])})
            execute("UPDATE settings SET value = %(v)s WHERE key = 'default_broker_commission_pct'",
                    {"v": float(request.form["default_broker_commission_pct"])})
            flash("Platform settings updated.", "success")
        return redirect(url_for("admin_dashboard"))

    pending_sellers = query(
        """SELECT u.id, u.full_name, u.email, sp.company_name FROM users u
           JOIN seller_profiles sp ON sp.user_id = u.id WHERE sp.approved = FALSE"""
    )
    pending_brokers = query(
        """SELECT u.id, u.full_name, u.email, bp.commission_rate FROM users u
           JOIN broker_profiles bp ON bp.user_id = u.id WHERE bp.approved = FALSE"""
    )
    all_brokers = query(
        """SELECT u.id, u.full_name, bp.commission_rate, bp.approved FROM users u
           JOIN broker_profiles bp ON bp.user_id = u.id ORDER BY u.full_name"""
    )
    transactions = query(
        """SELECT t.*, o.customer_id, o.seller_id FROM transactions t
           JOIN orders o ON o.id = t.order_id ORDER BY t.created_at DESC LIMIT 100"""
    )
    pending_payouts = query(
        """SELECT p.*, u.full_name, u.role FROM payouts p
           JOIN users u ON u.id = p.recipient_id WHERE p.status = 'pending'
           ORDER BY p.created_at DESC"""
    )
    totals = query(
        """SELECT
             COALESCE(SUM(total_amount) FILTER (WHERE status != 'pending_payment'), 0) AS gross_sales,
             COALESCE(SUM(platform_fee) FILTER (WHERE status != 'pending_payment'), 0) AS platform_revenue,
             COALESCE(SUM(commission_amount) FILTER (WHERE status != 'pending_payment'), 0) AS broker_commissions,
             COUNT(*) FILTER (WHERE status = 'paid') AS paid_orders
           FROM orders""",
        fetch="one",
    )
    settings_rows = query("SELECT key, value FROM settings")
    settings = {row["key"]: row["value"] for row in settings_rows}

    return render_template(
        "admin_dashboard.html", pending_sellers=pending_sellers, pending_brokers=pending_brokers,
        all_brokers=all_brokers, transactions=transactions, pending_payouts=pending_payouts,
        totals=totals, settings=settings,
    )


# ---------------------------------------------------------------------
# M-Pesa callback - Safaricom calls this directly, no auth (per Daraja spec)
# ---------------------------------------------------------------------
@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    payload = request.get_json(force=True, silent=True) or {}
    mpesa.process_callback(payload)
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=(config.MPESA_ENV == "sandbox"))
