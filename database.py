import sqlite3
import os
from datetime import datetime

DB_PATH = "business_assistant.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Documents table for auto-filing and dashboard
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_name TEXT,
            new_name TEXT,
            vendor TEXT,
            invoice_date TEXT,
            total REAL,
            category TEXT,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Chat history table for persistent memory
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # User Profile table for personalization
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            user_name TEXT,
            company_name TEXT,
            company_description TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

def save_document(original_name, new_name, vendor, invoice_date, total, category):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO documents (original_name, new_name, vendor, invoice_date, total, category)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (original_name, new_name, vendor, invoice_date, total, category))
    conn.commit()
    conn.close()

def get_dashboard_data():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. KPIs
    cursor.execute('SELECT SUM(total), COUNT(id) FROM documents')
    kpis = cursor.fetchone()
    total_spent = kpis[0] if kpis[0] else 0.0
    invoice_count = kpis[1] if kpis[1] else 0
    
    # 2. Monthly Spending Trend (Bar Chart)
    cursor.execute('SELECT substr(invoice_date, 1, 7) as month, SUM(total) FROM documents GROUP BY month ORDER BY month ASC')
    by_month = cursor.fetchall()
    
    # 3. Get spending by vendor (Top 5 for Pie Chart)
    cursor.execute('SELECT vendor, SUM(total) FROM documents GROUP BY vendor ORDER BY SUM(total) DESC LIMIT 5')
    by_vendor = cursor.fetchall()
    
    # 4. Recent Invoices
    cursor.execute('SELECT vendor, invoice_date, total, category FROM documents ORDER BY id DESC LIMIT 5')
    recent_invoices = cursor.fetchall()
    
    conn.close()
    
    # Format month strings (e.g. "2026-05" -> "May 2026")
    formatted_month = {}
    for r in by_month:
        try:
            # handle cases where date might be invalid or missing
            if r[0] and len(r[0]) >= 7:
                date_obj = datetime.strptime(r[0], "%Y-%m")
                formatted_month[date_obj.strftime("%b %Y")] = r[1]
            else:
                formatted_month["Unknown"] = r[1]
        except Exception:
            formatted_month[str(r[0])] = r[1]
            
    return {
        "kpi": {"total_spent": total_spent, "invoice_count": invoice_count},
        "monthly": formatted_month,
        "vendors": dict(by_vendor),
        "recent": [{"vendor": r[0], "date": r[1], "amount": r[2], "category": r[3]} for r in recent_invoices]
    }

def save_chat_message(session_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO chat_history (session_id, role, content)
        VALUES (?, ?, ?)
    ''', (session_id, role, content))
    conn.commit()
    conn.close()

def get_chat_history(session_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT role, content FROM chat_history 
        WHERE session_id = ? 
        ORDER BY timestamp DESC LIMIT ?
    ''', (session_id, limit))
    rows = cursor.fetchall()
    conn.close()
    # Reverse to get chronological order
    return rows[::-1]

def get_profile():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_name, company_name, company_description FROM user_profile WHERE id = 1')
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"user_name": row[0], "company_name": row[1], "company_description": row[2]}
    return {"user_name": "", "company_name": "", "company_description": ""}

def save_profile(user_name, company_name, company_description):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_profile (id, user_name, company_name, company_description)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            user_name=excluded.user_name,
            company_name=excluded.company_name,
            company_description=excluded.company_description
    ''', (user_name, company_name, company_description))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
