"""Έλεγχος προτάσεων χαρακτηρισμού ανά εταιρεία + μετάπτωσης από το παλιό κοινό σχήμα.
Εκτέλεση: python test_rules_per_company.py"""
import os
import sqlite3
import tempfile

os.environ["MYDATA_DATA_DIR"] = tempfile.mkdtemp()
import db  # noqa: E402

# 1) Παλιά βάση: κοινές προτάσεις (suppliers.rule_* + supplier_rules χωρίς company_id).
with sqlite3.connect(db.DB_PATH) as c:
    c.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL,
            aade_user_id TEXT, aade_subscription_key TEXT, aade_vat_number TEXT,
            mydata_env TEXT DEFAULT 'prod', use_accountant INTEGER DEFAULT 0);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE suppliers (vat TEXT PRIMARY KEY, name TEXT, rule_type TEXT,
            rule_category TEXT, rule_vat_type TEXT);
        CREATE TABLE supplier_rules (vat TEXT NOT NULL, vat_category TEXT NOT NULL,
            rule_category TEXT, rule_type TEXT, rule_vat_type TEXT, PRIMARY KEY (vat, vat_category));
        CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'expense', mark TEXT NOT NULL, counterparty_vat TEXT);
        INSERT INTO companies (company_name) VALUES ('Α'), ('Β');
        INSERT INTO settings VALUES ('active_company_id', '2');
        INSERT INTO suppliers VALUES ('111', 'Προμ. Α', 'E3_102_001', 'category2_1', 'VAT_361');
        INSERT INTO suppliers VALUES ('222', 'Χωρίς παραστατικό', 'E3_585_016', 'category2_5', '');
        INSERT INTO supplier_rules VALUES ('111', '1', 'category2_1', 'E3_102_001', 'VAT_361');
        INSERT INTO documents (company_id, mark, counterparty_vat) VALUES (1, 'm1', '111');
    """)
db.init_db()
db.init_db()  # idempotent

assert db.load_names() == {"111": "Προμ. Α", "222": "Χωρίς παραστατικό"}
assert db.load_rules(1) == {"111": {"category": "category2_1", "type": "E3_102_001", "vat_type": "VAT_361"}}
assert db.load_rule_patterns(1) == {"111": {"1": {"category": "category2_1", "type": "E3_102_001", "vat_type": "VAT_361"}}}
# ΑΦΜ χωρίς παραστατικό → στην ενεργή εταιρεία (2)
assert db.load_rules(2) == {"222": {"category": "category2_5", "type": "E3_585_016", "vat_type": ""}}
assert db.load_rule_patterns(2) == {}

# 2) Η αποθήκευση σε μία εταιρεία δεν αγγίζει την άλλη.
db.save_rule(2, "111", "E3_881_001", "category2_4")
assert db.load_rules(1)["111"]["type"] == "E3_102_001"
assert db.load_rules(2)["111"]["type"] == "E3_881_001"

# 3) Fallback από άλλη εταιρεία.
f = db.foreign_rule(2, "222")
assert f is None, f  # το 222 υπάρχει μόνο στην ίδια την εταιρεία 2
f = db.foreign_rule(1, "222")
assert f["company_name"] == "Β" and f["default"]["type"] == "E3_585_016" and f["patterns"] == {}
assert db.foreign_rule(1, "999") is None

# 4) Κατάλογος ανά εταιρεία: μόνο όσοι έχουν παραστατικά ή πρόταση της εταιρείας.
db.upsert_supplier("333", "Χωρίς συναλλαγές")
assert [s["vat"] for s in db.list_suppliers(1)] == ["111"]
assert sorted(s["vat"] for s in db.list_suppliers(2)) == ["111", "222"]
assert len(db.list_suppliers()) == 3  # εξαγωγή: όλος ο κοινός κατάλογος

# 5) Διαγραφή συναλλασσόμενου → φεύγουν οι προτάσεις όλων των εταιρειών.
db.delete_supplier("111")
assert "111" not in db.load_rules(1) and "111" not in db.load_rules(2)
print("OK")
