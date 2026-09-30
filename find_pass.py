import psycopg2

passwords = ["postgres", "admin", "root", "1234", "123456", "password", "p@ssword", ""]
ports = [5432, 5433]

found = False

for port in ports:
    for pwd in passwords:
        try:
            conn = psycopg2.connect(
                dbname="postgres",
                user="postgres",
                password=pwd,
                host="127.0.0.1",
                port=port,
                connect_timeout=2
            )
            print(f" SUCCESS! Connected with port={port} and password='{pwd}'")
            conn.close()
            found = True
            break
        except Exception:
            continue
    if found:
        break

if not found:
    print("❌ None of the standard default passwords worked.")
    