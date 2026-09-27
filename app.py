import sqlite3
import re
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pdfplumber
from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)
# Replace this development key with a private random value before deployment.
app.secret_key = "replace-this-with-a-random-secret-key"
DATABASE = Path(app.root_path) / "budget.db"
UPLOAD_FOLDER = Path(app.root_path) / "uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)


def save_temporary_upload(uploaded_file, extension):
    """Save an upload under a random safe name inside the uploads directory."""
    safe_name = secure_filename(uploaded_file.filename or "")
    if not safe_name or Path(safe_name).suffix.lower() != extension:
        raise ValueError("Unsupported format")

    temp_path = UPLOAD_FOLDER / f"{uuid.uuid4().hex}{extension}"
    uploaded_file.save(temp_path)
    if temp_path.stat().st_size == 0:
        temp_path.unlink(missing_ok=True)
        raise ValueError("Empty file")
    return temp_path


def get_db_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user'
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                food INTEGER NOT NULL,
                rent INTEGER NOT NULL,
                other INTEGER NOT NULL,
                total INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT 'expense',
                amount INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)

        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(users)")
        }
        if "role" not in user_columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
            )

        # Add user_id to databases created by the earlier single-user version.
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(transactions)")
        }
        if "user_id" not in columns:
            connection.execute("ALTER TABLE transactions ADD COLUMN user_id INTEGER")
        new_columns = {
            "type": "TEXT NOT NULL DEFAULT 'expense'",
            "amount": "INTEGER NOT NULL DEFAULT 0",
            "category": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in new_columns.items():
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE transactions ADD COLUMN {column} {definition}"
                )
        connection.execute(
            "UPDATE transactions SET amount = total "
            "WHERE amount = 0 AND type = 'expense' AND total > 0"
        )

        # Keep the original demo account available for the first login.
        connection.execute(
            "INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)",
            ("admin", "admin123", "admin"),
        )
        connection.execute(
            "UPDATE users SET role = 'admin' WHERE username = 'admin'"
        )


init_db()


def login_required():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return None


def get_expense_buckets(amount, category, description):
    category_text = (category or "").strip().lower()
    description_text = (description or "").lower()
    if category_text == "food" or any(
        word in description_text for word in ("cafe", "juice", "swiggy", "zomato", "restaurant")
    ):
        return amount, 0, 0, "food"
    if category_text == "rent" or any(
        word in description_text for word in ("rent", "house")
    ):
        return 0, amount, 0, "rent"
    if category_text in {"bills", "shopping", "other"}:
        return 0, 0, amount, category_text
    return 0, 0, amount, "other"


@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db_connection() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE username = ? AND password = ?",
                (username, password),
            ).fetchone()

        if user:
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("dashboard"))
        error = "Invalid credentials"

    return render_template("login.html", error=error)


@app.route("/add-user", methods=["GET", "POST"])
def add_user():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            error = "Enter a username and password."
        else:
            try:
                with get_db_connection() as connection:
                    connection.execute(
                        "INSERT INTO users (username, password) VALUES (?, ?)",
                        (username, password),
                    )
                flash("User created successfully.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "That username already exists."

    return render_template("add_user.html", error=error)


@app.route("/admin")
def admin():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))

    with get_db_connection() as connection:
        users = connection.execute(
            "SELECT id, username, role FROM users ORDER BY id"
        ).fetchall()
    return render_template("admin.html", users=users, username=session.get("username", ""))


@app.route("/delete-user/<int:user_id>", methods=["POST"])
def delete_user(user_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "admin":
        return redirect(url_for("dashboard"))
    if user_id == session["user_id"]:
        flash("You cannot delete your own admin account.", "error")
        return redirect(url_for("admin"))

    with get_db_connection() as connection:
        connection.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
        result = connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    if result.rowcount:
        flash("User and their transactions deleted.", "success")
    else:
        flash("User not found.", "error")
    return redirect(url_for("admin"))


@app.route("/calculate", methods=["POST"])
def calculate():
    protected = login_required()
    if protected:
        return protected

    try:
        submitted_date = date.fromisoformat(request.form["date"]).isoformat()
        food = int(request.form["food_expense"])
        rent = int(request.form["rent"])
        other = int(request.form["other_expense"])
        if min(food, rent, other) < 0:
            raise ValueError
    except (KeyError, ValueError):
        flash("Enter a valid date and non-negative whole-number expenses.", "error")
        return redirect(url_for("dashboard"))

    total = food + rent + other
    with get_db_connection() as connection:
        connection.execute(
            "INSERT INTO transactions "
            "(user_id, date, food, rent, other, total, type, amount, category, description) "
            "VALUES (?, ?, ?, ?, ?, ?, 'expense', ?, 'manual budget', 'Manual budget entry')",
            (session["user_id"], submitted_date, food, rent, other, total, total),
        )
    flash("Transaction saved.", "success")
    return redirect(url_for("dashboard"))


@app.route("/add-transaction", methods=["POST"])
def add_transaction():
    protected = login_required()
    if protected:
        return protected

    try:
        submitted_date = date.fromisoformat(request.form["date"]).isoformat()
        amount = int(request.form["amount"])
        transaction_type = request.form["type"].strip().lower()
        if amount <= 0 or transaction_type not in {"income", "expense"}:
            raise ValueError
    except (KeyError, ValueError):
        flash("Enter a valid date, positive amount, and transaction type.", "error")
        return redirect(url_for("dashboard"))

    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    if transaction_type == "income":
        food = rent = other = total = 0
        category = category or "income"
    else:
        food, rent, other, category = get_expense_buckets(
            amount, category, description
        )
        total = amount

    with get_db_connection() as connection:
        connection.execute(
            "INSERT INTO transactions "
            "(user_id, date, food, rent, other, total, type, amount, category, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session["user_id"], submitted_date, food, rent, other, total,
                transaction_type, amount, category, description,
            ),
        )
    flash("Transaction added.", "success")
    return redirect(url_for("dashboard"))


@app.route("/import", methods=["GET", "POST"])
def import_statement():
    protected = login_required()
    if protected:
        return protected

    error = None
    if request.method == "POST":
        uploaded_file = request.files.get("file")
        if not uploaded_file or not uploaded_file.filename:
            error = "Empty file"
        else:
            temp_path = None
            try:
                temp_path = save_temporary_upload(uploaded_file, ".xlsx")
                try:
                    df = pd.read_excel(temp_path, engine="openpyxl")
                except Exception as exc:
                    raise ValueError(f"Excel read error: {exc}") from exc

                if df.empty:
                    raise ValueError("Empty file")

                print("Excel columns:", list(df.columns))
                df.columns = [str(column).strip().lower() for column in df.columns]
                print(df.head())

                date_column = df.get("date")
                description_column = df.get("description")
                amount_column = df.get("amount")
                if date_column is None or description_column is None or amount_column is None:
                    raise ValueError(
                        "Columns missing. Expected columns: Date, Description, Amount"
                    )

                food_total = rent_total = other_total = 0
                valid_rows = 0
                for _, row in df.iterrows():
                    try:
                        amount = int(round(float(row["amount"])))
                    except (TypeError, ValueError, OverflowError):
                        continue

                    description = (
                        "" if pd.isna(row["description"])
                        else str(row["description"]).lower()
                    )
                    if any(word in description for word in ("swiggy", "zomato", "restaurant")):
                        food_total += amount
                    elif any(word in description for word in ("rent", "house")):
                        rent_total += amount
                    else:
                        other_total += amount
                    valid_rows += 1

                if valid_rows == 0:
                    raise ValueError("Parsing failed: no rows had a valid Amount.")

                total = food_total + rent_total + other_total
                with get_db_connection() as connection:
                    connection.execute(
                        "INSERT INTO transactions "
                        "(user_id, date, food, rent, other, total, type, amount, category, description) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'expense', ?, 'statement import', 'Excel statement import')",
                        (session["user_id"], date.today().isoformat(), food_total,
                         rent_total, other_total, total, total),
                    )
                return render_template(
                    "import.html",
                    username=session.get("username", ""),
                    summary={"food": food_total, "rent": rent_total,
                             "other": other_total, "total": total},
                )
            except ValueError as exc:
                error = str(exc)
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink(missing_ok=True)

    return render_template("import.html", error=error, username=session.get("username", ""))


@app.route("/import-pdf", methods=["GET", "POST"])
def import_pdf():
    protected = login_required()
    if protected:
        return protected

    error = None
    if request.method == "POST":
        uploaded_file = request.files.get("file")
        if not uploaded_file or not uploaded_file.filename:
            error = "Empty file"
        else:
            temp_path = None
            try:
                temp_path = save_temporary_upload(uploaded_file, ".pdf")
                try:
                    with pdfplumber.open(temp_path) as pdf:
                        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
                except Exception as exc:
                    raise ValueError(f"PDF read error: {exc}") from exc

                print(text[:500])
                if not text.strip():
                    raise ValueError("No text found in PDF")

                entries = []
                food_total = rent_total = other_total = income_total = 0
                date_pattern = re.compile(r"^\d{2}-\d{2}-\d{4}")
                amount_pattern = re.compile(
                    r"(\d[\d,]*\.\d{2})\s*(Dr|Cr)\b", re.IGNORECASE
                )

                for line in text.split("\n"):
                    line = line.strip()
                    if not line or not date_pattern.match(line):
                        continue

                    amount_match = amount_pattern.search(line)
                    if not amount_match:
                        continue

                    try:
                        entry_date = datetime.strptime(
                            line[:10], "%d-%m-%Y"
                        ).date().isoformat()
                        amount = int(round(float(amount_match.group(1).replace(",", ""))))
                    except (ValueError, OverflowError):
                        continue

                    description = line[10:amount_match.start()].strip().lower()
                    transaction_type = amount_match.group(2).lower()
                    if transaction_type == "cr":
                        transaction_type = "income"
                        category = "income"
                        food = rent = other = 0
                        total = 0
                        income_total += amount
                    else:
                        transaction_type = "expense"
                        food, rent, other, category = get_expense_buckets(
                            amount, "", description
                        )
                        total = amount
                        if category == "food":
                            food_total += amount
                        elif category == "rent":
                            rent_total += amount
                        else:
                            other_total += amount

                    entries.append({
                        "date": entry_date,
                        "food": food,
                        "rent": rent,
                        "other": other,
                        "total": total,
                        "amount": amount,
                        "type": transaction_type,
                        "category": category,
                        "description": description,
                    })

                print("Parsed PDF entries:", entries[:10])
                if not entries:
                    raise ValueError("Parsing failed: no valid debit transaction lines were found.")

                with get_db_connection() as connection:
                    connection.executemany(
                        "INSERT INTO transactions "
                        "(user_id, date, food, rent, other, total, type, amount, category, description) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                session["user_id"], entry["date"], entry["food"],
                                entry["rent"], entry["other"], entry["total"],
                                entry["type"], entry["amount"], entry["category"],
                                entry["description"],
                            )
                            for entry in entries
                        ],
                    )

                return render_template(
                    "import_pdf.html",
                    username=session.get("username", ""),
                    summary={
                        "count": len(entries),
                        "food": food_total,
                        "rent": rent_total,
                        "other": other_total,
                        "income": income_total,
                        "expense": food_total + rent_total + other_total,
                    },
                )
            except ValueError as exc:
                error = str(exc)
            finally:
                if temp_path and temp_path.exists():
                    temp_path.unlink(missing_ok=True)

    return render_template("import_pdf.html", error=error, username=session.get("username", ""))


@app.route("/dashboard")
def dashboard():
    protected = login_required()
    if protected:
        return protected

    user_id = session["user_id"]
    with get_db_connection() as connection:
        totals = connection.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) AS total_income,
                COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS total_expense
            FROM transactions
            WHERE user_id = ?
        """, (user_id,)).fetchone()
        monthly_rows = connection.execute("""
            SELECT
                strftime('%Y-%m', date) AS month,
                SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END) AS income_total,
                SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) AS expense_total
            FROM transactions
            WHERE user_id = ?
            GROUP BY strftime('%Y-%m', date)
            ORDER BY month ASC
        """, (user_id,)).fetchall()
        last_transaction = connection.execute("""
            SELECT date, amount, type, category, description
            FROM transactions
            WHERE user_id = ?
            ORDER BY date DESC, id DESC
            LIMIT 1
        """, (user_id,)).fetchone()

    monthly_totals = [dict(row) for row in monthly_rows]
    highest_month = max(
        monthly_totals,
        key=lambda row: row["expense_total"],
        default=None,
    )
    balance = totals["total_income"] - totals["total_expense"]
    return render_template(
        "dashboard.html",
        error=request.args.get("error"),
        message=request.args.get("message"),
        monthly_totals=monthly_totals,
        total_income=totals["total_income"],
        total_expense=totals["total_expense"],
        balance=balance,
        last_transaction=last_transaction,
        highest_month=highest_month,
        username=session.get("username", ""),
    )


@app.route("/history")
def history():
    protected = login_required()
    if protected:
        return protected

    with get_db_connection() as connection:
        transactions = connection.execute("""
            SELECT * FROM transactions
            WHERE user_id = ?
            ORDER BY date DESC, id DESC
        """, (session["user_id"],)).fetchall()
    return render_template(
        "history.html",
        transactions=transactions,
        username=session.get("username", ""),
    )


@app.route("/logout")
def logout():
    protected = login_required()
    if protected:
        return protected
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True)
