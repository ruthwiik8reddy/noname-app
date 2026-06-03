from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
import os

app = Flask(__name__)
app.secret_key = "autofiera-secret-key"

DB_PATH = "studios.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route("/")
def index():
    if "logged_in" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        conn = get_db()
        studio = conn.execute(
            "SELECT * FROM studios WHERE username = ? AND password = ?",
            (username, password)
        ).fetchone()
        conn.close()

        if studio:
            session["logged_in"] = True
            session["studio_id"] = studio["id"]
            session["studio"]    = studio["name"]
            session["city"]      = studio["city"]
            session["logo"]      = studio["logo"]
            session["owner"]     = studio["owner"]
            return redirect(url_for("dashboard"))
        error = "Wrong username or password. Please try again."

    return render_template("login.html", error=error)

@app.route("/dashboard")
def dashboard():
    if "logged_in" not in session:
        return redirect(url_for("login"))
    return render_template("dashboard.html",
        studio = session["studio"],
        city   = session["city"],
        logo   = session["logo"],
        owner  = session["owner"],
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True, port=5055)
