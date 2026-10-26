"""
Script to import all sheets from northwind_data.xlsx into a permanent SQLite database (northwind.db).
"""
import os
import re
import sqlite3
import openpyxl

XLSX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "northwind_data.xlsx")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "northwind.db")

def clean_column_name(col_name: str) -> str:
    """Converts Excel header text into clean SQLite column name."""
    if not col_name:
        return "unnamed_column"
    col = str(col_name).strip().lower()
    col = re.sub(r'[^a-z0-9_]+', '_', col)
    col = re.sub(r'_+', '_', col).strip('_')
    return col or "column"

def convert_excel_to_sqlite(xlsx_path: str = XLSX_PATH, db_path: str = DB_PATH):
    print(f"Loading workbook from: {xlsx_path}")
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]
        table_name = clean_column_name(sheet_name)
        
        # Read rows
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            print(f"Skipping empty sheet: {sheet_name}")
            continue

        raw_headers = rows[0]
        headers = [clean_column_name(h) for h in raw_headers]
        
        # Handle duplicates in headers
        seen = {}
        unique_headers = []
        for h in headers:
            if h in seen:
                seen[h] += 1
                unique_headers.append(f"{h}_{seen[h]}")
            else:
                seen[h] = 0
                unique_headers.append(h)

        data_rows = rows[1:]
        # Filter out completely empty rows
        data_rows = [r for r in data_rows if any(cell is not None for cell in r)]

        print(f"Processing sheet '{sheet_name}' -> table '{table_name}' ({len(data_rows)} rows, {len(unique_headers)} cols)...")

        # Drop existing table
        cursor.execute(f"DROP TABLE IF EXISTS {table_name}")

        # Create table with TEXT columns (preserving all types safely)
        col_defs = ", ".join([f'"{h}" TEXT' for h in unique_headers])
        create_sql = f'CREATE TABLE {table_name} ({col_defs})'
        cursor.execute(create_sql)

        # Insert rows
        placeholders = ", ".join(["?"] * len(unique_headers))
        insert_sql = f'INSERT INTO {table_name} VALUES ({placeholders})'

        cleaned_rows = []
        for row in data_rows:
            cleaned_row = []
            for idx in range(len(unique_headers)):
                h_name = unique_headers[idx].lower()
                val = row[idx] if idx < len(row) else None
                if val is None:
                    cleaned_row.append(None)
                else:
                    str_val = str(val).strip()
                    # Convert email addresses to lowercase (e.g. 'email', 'ops_contact', or any text with @)
                    if h_name in ("email", "ops_contact") or ("@" in str_val and " " not in str_val):
                        cleaned_row.append(str_val.lower())
                    else:
                        cleaned_row.append(str_val)
            cleaned_rows.append(cleaned_row)

        cursor.executemany(insert_sql, cleaned_rows)

    # Add useful indexes if standard tables exist
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipments_id ON shipments(shipment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipments_account ON shipments(account_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_shipment_id ON events(shipment_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_accounts_id ON accounts(account_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email)")

    conn.commit()
    conn.close()
    print(f"Successfully converted all sheets into SQLite: {db_path}")

if __name__ == '__main__':
    convert_excel_to_sqlite()
