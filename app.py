from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = "autofiera-secret-key"

# Single studio for now — we'll move this to a database later
STUDIO = {
    "name": "Shine Pro Detailing",
    "city": "Los Angeles, CA",
    "logo": "shinepro_logo.png",
    "username": "admin",
    "password": "shine123",
}

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
        if username == STUDIO["username"] and password == STUDIO["password"]:
            session["logged_in"] = True
            session["studio"] = STUDIO["name"]
            session["city"] = STUDIO["city"]
            session["logo"] = STUDIO["logo"]
            return redirect(url_for("dashboard"))
        error = "Wrong username or password."
    return render_template("login.html", error=error)

@app.route("/dashboard")
def dashboard():
    if "logged_in" not in session:
        return redirect(url_for("login"))
    return render_template("dashboard.html",
                           studio=session["studio"],
                           city=session["city"],
                           logo=session["logo"])

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True, port=5055)
