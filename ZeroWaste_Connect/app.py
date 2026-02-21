from flask import Flask, render_template, request, redirect, session, flash, url_for
import sqlite3
import math
import requests

app = Flask(__name__)
app.secret_key = "zerowaste_secret"

WHATSAPP_TOKEN = "EAASUNQhBunIBQz5rKJG4wZCR7s7Tf7q2ZCGU1jGTtHckeh7QXe0UgSyBSOwyk8QJTOSKtD1WsM6zKZBeNwmteTL4MELQungTLYu8BZBiZCFrBnb90KhT11pZBUekLoC1AhmT1bPTPAxMxiNeKl5g657cuT6NJAiIbmTwS9PUuAUu9UzZA1eBZBj2PyBZBYZA3roHhnGwZDZD"
WHATSAPP_PHONE_ID = "1036215726237967"
WEBHOOK_VERIFY_TOKEN = "zerowaste_webhook"

# ---------------- DATABASE INITIALIZATION ---------------- #
def init_db():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    # Create users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    password TEXT,
                    role TEXT,
                    latitude REAL,
                    longitude REAL,
                    capacity INTEGER,
                    original_capacity INTEGER,
                    phone TEXT
                )''')
    # Create surplus table
    c.execute('''CREATE TABLE IF NOT EXISTS surplus (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    restaurant_id INTEGER,
                    food_name TEXT,
                    quantity INTEGER,
                    expiry_hours INTEGER,
                    assigned_ngo_id INTEGER,
                    distance REAL,
                    status TEXT
                )''')
    conn.commit()

    # Safely add phone column if upgrading from old DB
    try:
        c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  

    conn.close()

# ---------------- HELPERS ---------------- #
def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371
    dLat, dLon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dLat/2)**2 + math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) * math.sin(dLon/2)**2)
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))

def match_ngo(expiry_hours, quantity, provider_name):
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT latitude, longitude FROM users WHERE name=?", (provider_name,))
    res = c.fetchone()
    if not res:
        conn.close()
        return None

    r_lat, r_lon = res
    c.execute("SELECT id, name, latitude, longitude, capacity FROM users WHERE role='ngo' AND capacity >= ?", (quantity,))
    ngos = c.fetchall()

    best_score, best_ngo = None, None
    for ngo_id, name, lat, lon, cap in ngos:
        dist = calculate_distance(r_lat, r_lon, lat, lon)
        score = dist + (expiry_hours * 0.5)
        if best_score is None or score < best_score:
            best_score, best_ngo = score, name
    conn.close()
    return best_ngo

def get_home_stats():
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    try:
        # 1. Total meals collected
        meals = c.execute("SELECT COALESCE(SUM(quantity), 0) FROM surplus WHERE status='Collected'").fetchone()[0]
        
        # 2. Total Partner NGOs
        ngos = c.execute("SELECT COUNT(*) FROM users WHERE role='ngo'").fetchone()[0]
        
        # 3. Total Restaurants (only)
        restaurants = c.execute("SELECT COUNT(*) FROM users WHERE role='restaurant'").fetchone()[0]
        
        # 4. Total Individual Donors (only)
        donors = c.execute("SELECT COUNT(*) FROM users WHERE role='donor'").fetchone()[0]
        
    except sqlite3.OperationalError:
        meals, ngos, restaurants, donors = 0, 0, 0, 0
    finally:
        conn.close()
        
    return int(meals), int(ngos), int(restaurants), int(donors)

def send_whatsapp_message(to_phone, ngo_name, food_name, quantity, expiry_hours, provider_name, distance):
    url = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_ID}/messages"
    message = (
        f"🍱 *New Food Donation Alert!*\n\n"
        f"Hello *{ngo_name}*,\n\n"
        f"A surplus food donation has been matched to you:\n\n"
        f"🏪 *From:* {provider_name}\n"
        f"🥘 *Food:* {food_name}\n"
        f"📦 *Quantity:* {quantity} units\n"
        f"⏰ *Expires in:* {expiry_hours} hours\n"
        f"📍 *Distance:* {distance:.2f} KM away\n\n"
        f"Please arrange collection soon!\n\n— ZeroWaste Connect 🌱"
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message}
    }
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        return response.status_code == 200
    except Exception:
        return False

# ---------------- ROUTES ---------------- #
@app.route("/")
def home():
    # Unpack all four stats
    meals, ngos, restaurants, donors = get_home_stats()
    
    # Send them all to home.html
    return render_template("home.html", 
                           meals=meals, 
                           ngos=ngos, 
                           restaurants=restaurants, 
                           donors=donors)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        role = request.form.get("role")
        name = request.form.get("name")
        password = request.form.get("password")
        
        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("SELECT role FROM users WHERE name=? AND password=? AND role=?", (name, password, role))
        user = c.fetchone()
        conn.close()

        if user:
            session["user"], session["role"] = name, role
            return redirect(url_for("dashboard")) if role == "ngo" else redirect(url_for("add_surplus"))
        
        flash("Invalid credentials!")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "user" not in session or session["role"] != "ngo":
        return redirect(url_for("login"))
    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    c.execute("SELECT id, capacity, original_capacity FROM users WHERE name=?", (session["user"],))
    ngo_id, cap, o_cap = c.fetchone()
    used_pct = round(((o_cap - cap) / o_cap * 100), 2) if o_cap > 0 else 0

    c.execute("""SELECT s.id, u.name, s.food_name, s.quantity, s.expiry_hours, s.distance, s.status
                 FROM surplus s JOIN users u ON s.restaurant_id = u.id
                 WHERE s.assigned_ngo_id = ?""", (ngo_id,))
    records = c.fetchall()

    total     = c.execute("SELECT COUNT(*) FROM surplus").fetchone()[0]
    collected = c.execute("SELECT COUNT(*) FROM surplus WHERE status='Collected'").fetchone()[0]
    assigned  = c.execute("SELECT COUNT(*) FROM surplus WHERE status='Assigned'").fetchone()[0]

    conn.close()
    return render_template("surplus.html", records=records, capacity=cap, ngo_name=session["user"],
                           total_surplus=total, total_collected=collected,
                           total_assigned=assigned, used_percentage=used_pct)

@app.route("/add_surplus", methods=["GET", "POST"])
def add_surplus():
    allowed_roles = ["restaurant", "donor"]
    if "user" not in session or session["role"] not in allowed_roles:
        flash("Unauthorized access.")
        return redirect(url_for("login"))

    if request.method == "POST":
        f_name = request.form.get("food_name")
        qty    = int(request.form.get("quantity"))
        exp    = int(request.form.get("expiry"))

        ngo_name = match_ngo(exp, qty, session["user"])
        if not ngo_name:
            flash("No NGO available with sufficient capacity!")
            return redirect(url_for("add_surplus"))

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        c.execute("SELECT id, latitude, longitude FROM users WHERE name=?", (session["user"],))
        p_id, p_lat, p_lon = c.fetchone()

        c.execute("SELECT id, latitude, longitude, phone FROM users WHERE name=?", (ngo_name,))
        n_id, n_lat, n_lon, ngo_phone = c.fetchone()

        dist = calculate_distance(p_lat, p_lon, n_lat, n_lon)

        c.execute("""INSERT INTO surplus (restaurant_id, food_name, quantity, expiry_hours,
                      assigned_ngo_id, distance, status) VALUES (?, ?, ?, ?, ?, ?, 'Assigned')""",
                  (p_id, f_name, qty, exp, n_id, dist))
        
        c.execute("UPDATE users SET capacity = capacity - ? WHERE id = ?", (qty, n_id))
        conn.commit()
        conn.close()

        send_whatsapp_message(ngo_phone, ngo_name, f_name, qty, exp, session["user"], dist)
        flash(f"✅ Matched to {ngo_name} ({dist:.2f} KM away)!")

    return render_template("add_surplus.html")

@app.route("/update_capacity", methods=["POST"])
def update_capacity():
    if "user" in session and session["role"] == "ngo":
        new_cap = request.form.get("capacity")
        if new_cap:
            conn = sqlite3.connect("database.db")
            c = conn.cursor()
            # Update both current and original capacity
            c.execute("UPDATE users SET capacity = ?, original_capacity = ? WHERE name = ?",
                      (int(new_cap), int(new_cap), session["user"]))
            conn.commit()
            conn.close()
            flash("Capacity updated successfully!")
    return redirect(url_for("dashboard"))

@app.route("/collect/<int:surplus_id>")
def mark_collected(surplus_id):
    if "user" not in session or session["role"] != "ngo":
        return redirect(url_for("login"))

    conn = sqlite3.connect("database.db")
    c = conn.cursor()
    
    # Verify the NGO calling this is the one the surplus was assigned to
    c.execute("""SELECT s.quantity, s.assigned_ngo_id 
                 FROM surplus s JOIN users u ON s.assigned_ngo_id = u.id 
                 WHERE s.id=? AND u.name=?""", (surplus_id, session["user"]))
    item = c.fetchone()
    
    if item:
        c.execute("UPDATE surplus SET status='Collected' WHERE id=?", (surplus_id,))
        c.execute("UPDATE users SET capacity = capacity + ? WHERE id=?", (item[0], item[1]))
        conn.commit()
        flash("Food marked as collected!")
    
    conn.close()
    return redirect(url_for("dashboard"))

@app.route("/register_ngo", methods=["GET", "POST"])
def register_ngo():
    if request.method == "POST":
        data = request.form
        lat, lon = data.get("latitude"), data.get("longitude")
        if not lat or not lon:
            flash("Please select location on map.")
            return redirect(url_for("register_ngo"))

        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO users (name, password, role, latitude, longitude, capacity, original_capacity, phone)
                         VALUES (?, ?, 'ngo', ?, ?, ?, ?, ?)""",
                      (data['name'], data['password'], float(lat), float(lon),
                       int(data['capacity']), int(data['capacity']), data.get('phone')))
            conn.commit()
        except sqlite3.IntegrityError:
            flash("NGO name already exists.")
            return redirect(url_for("register_ngo"))
        finally:
            conn.close()
        return redirect(url_for("login"))
    return render_template("register_ngo.html")

@app.route("/register_restaurant", methods=["GET", "POST"])
def register_restaurant():
    if request.method == "POST":
        data = request.form
        lat, lon = data.get("latitude"), data.get("longitude")
        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO users (name, password, role, latitude, longitude, capacity, original_capacity)
                         VALUES (?, ?, 'restaurant', ?, ?, 0, 0)""",
                      (data['name'], data['password'], float(lat), float(lon)))
            conn.commit()
        except sqlite3.IntegrityError:
            flash("Name already exists.")
        finally:
            conn.close()
        return redirect(url_for("login"))
    return render_template("register_restaurant.html")

@app.route("/register_donor", methods=["GET", "POST"])
def register_donor():
    if request.method == "POST":
        data = request.form
        lat, lon = data.get("latitude"), data.get("longitude")
        phone = data.get("phone") # Get phone from form
        
        conn = sqlite3.connect("database.db")
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO users (name, password, role, latitude, longitude, capacity, original_capacity, phone)
                         VALUES (?, ?, 'donor', ?, ?, 0, 0, ?)""",
                      (data['name'], data['password'], float(lat), float(lon), phone))
            conn.commit()
        except sqlite3.IntegrityError:
            flash("Name already exists.")
        finally:
            conn.close()
        return redirect(url_for("login"))
    return render_template("register_donor.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

if __name__ == "__main__":
    init_db() # Create tables right before the app runs
    app.run(debug=True)
