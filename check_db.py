import psycopg2

conn = psycopg2.connect(
    dbname="market_place",
    user="postgres",
    password="YOUR_MATCHED_PASSWORD",  # Replace with the password from find_pass.py
    host="127.0.0.1",
    port=5432                          # Replace with the port from find_pass.py if it was 5433
)
cursor = conn.cursor()

# Get column names of users table
cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'users';")
columns = [row[0] for row in cursor.fetchall()]
print(f"COLUMNS IN 'users' TABLE: {columns}")

# Fetch all existing users
cursor.execute("SELECT * FROM users;")
rows = cursor.fetchall()
print("\nREGISTERED ACCOUNTS IN DATABASE:")
for row in rows:
    print(dict(zip(columns, row)))

conn.close()