"""Regenerates tests/backend/fixtures/quality/*.csv byte-exactly (run from the repo root). Expected results: test_quality_*.py."""

from pathlib import Path

d = Path("tests/backend/fixtures/quality")
d.mkdir(parents=True, exist_ok=True)

H = "Date,Invoice Date,Account,Vendor,Description,Invoice No,Amount,Dr/Cr,Currency\n"
CLEAN = [
    "2024-04-02,2024-03-30,5010,Acme Supplies Ltd,Office chairs,INV-1001,12500.00,Dr,INR\n",
    "2024-04-05,2024-04-01,5020,Blue Dart Express,Courier charges,INV-1002,1240.50,Dr,INR\n",
    "2024-04-09,2024-04-08,4000,Customer A,Receipt against invoice,RCPT-0077,50000.00,Cr,INR\n",
    "2024-04-15,2024-04-12,5010,Acme Supplies Ltd,Desk,INV-1003,8000.00,Dr,INR\n",
    "2024-04-22,2024-04-20,5030,City Power Co,Electricity April,INV-1004,15675.25,Dr,INR\n",
]

# Clean padding rows so a few bad values stay below mapping.max_unconvertible_share.
EXTRA = (
    "2024-04-23,2024-04-21,5040,Office Mart,Paper,INV-2001,310.00,Dr,INR\n"
    "2024-04-24,2024-04-21,5040,Office Mart,Toner,INV-2002,4200.00,Dr,INR\n"
    "2024-04-25,2024-04-21,5050,Telco Ltd,Phone bill,INV-2003,999.00,Dr,INR\n"
)

files = {
    # 1. clean: no issue at all
    "clean.csv": H + "".join(CLEAN),
    # 2. missing values: required posted_at x1, amount x2, direction x1; optional payee x1, description x2
    "missing_values.csv": H
    + "2024-04-02,2024-03-30,5010,Acme Supplies Ltd,Office chairs,INV-1001,12500.00,Dr,INR\n"
    + ",2024-04-01,5020,Blue Dart Express,Courier charges,INV-1002,1240.50,Dr,INR\n"
    + "2024-04-09,2024-04-08,4000,Customer A,,RCPT-0077,,Cr,INR\n"
    + "2024-04-15,2024-04-12,5010,,Desk,INV-1003,8000.00,,INR\n"
    + "2024-04-22,2024-04-20,5030,City Power Co,,INV-1004,,Dr,INR\n",
    # 3. invalid numerics: 'abc' (no reading), '1 234.50' (one other reading), '12.345' (too many decimals)
    "invalid_numeric.csv": H
    + CLEAN[0]
    + "2024-04-05,2024-04-01,5020,Blue Dart Express,Courier charges,INV-1002,abc,Dr,INR\n"
    + "2024-04-09,2024-04-08,4000,Customer A,Receipt against invoice,RCPT-0077,1 234.50,Cr,INR\n"
    + "2024-04-15,2024-04-12,5010,Acme Supplies Ltd,Desk,INV-1003,12.345,Dr,INR\n"
    + CLEAN[4]
    + EXTRA,
    # 4. invalid dates (declared %Y-%m-%d): no reading, ambiguous (2 readings), one other reading; + implausible
    "invalid_date.csv": H
    + CLEAN[0]
    + "2024-13-45,2024-04-01,5020,Blue Dart Express,Courier charges,INV-1002,1240.50,Dr,INR\n"
    + "05/04/2024,2024-04-08,4000,Customer A,Receipt against invoice,RCPT-0077,50000.00,Cr,INR\n"
    + "2024/04/07,2024-04-12,5010,Acme Supplies Ltd,Desk,INV-1003,8000.00,Dr,INR\n"
    + "1850-04-22,2024-04-20,5030,City Power Co,Electricity April,INV-1004,15675.25,Dr,INR\n"
    + EXTRA,
    # 5. exact duplicates: rows 2 and 5 repeat row 1 (one group of 3 → 2 redundant rows)
    "duplicates.csv": H + CLEAN[0] + CLEAN[0] + CLEAN[1] + CLEAN[2] + CLEAN[0],
    # 6. near duplicates: row 2 = row 1 one day later (new invoice no); row 5 ≈ row 4 with case/space variants;
    #    row 3 same vendor+amount but 10 days later (outside the 3-day window) → 2 near duplicates
    "near_duplicates.csv": H
    + "2024-04-02,2024-03-30,5010,Acme Supplies Ltd,Office chairs,INV-1001,12500.00,Dr,INR\n"
    + "2024-04-03,2024-03-31,5010,Acme Supplies Ltd,Office chairs,INV-1009,12500.00,Dr,INR\n"
    + "2024-04-13,2024-04-10,5010,Acme Supplies Ltd,Office chairs,INV-1010,12500.00,Dr,INR\n"
    + "2024-04-15,2024-04-12,5030,City Power Co,Electricity April,INV-1004,15675.25,Dr,INR\n"
    + "2024-04-17,2024-04-12,5030,CITY  POWER CO,Electricity April (copy),INV-1004A,15675.25,Dr,INR\n",
    # 7/8. negative x1, zero x2
    "negative_zero.csv": H
    + CLEAN[0]
    + "2024-04-05,2024-04-01,5020,Blue Dart Express,Credit note,CN-0001,-500.00,Cr,INR\n"
    + "2024-04-09,2024-04-08,4000,Customer A,Nil invoice,INV-0000,0.00,Dr,INR\n"
    + "2024-04-15,2024-04-12,5010,Acme Supplies Ltd,Free sample,INV-1003,0,Dr,INR\n"
    + CLEAN[4],
    # 9. out of period for a case period 2024-04-01..2024-04-30: 2024-03-31 and 2024-05-01
    "out_of_period.csv": H
    + "2024-03-31,2024-03-30,5010,Acme Supplies Ltd,Office chairs,INV-1001,12500.00,Dr,INR\n"
    + CLEAN[1]
    + CLEAN[2]
    + "2024-05-01,2024-04-12,5010,Acme Supplies Ltd,Desk,INV-1003,8000.00,Dr,INR\n"
    + CLEAN[4],
    # 10. duplicate IDs: 'Txn ID' T1 repeated once (unmapped ID column), Invoice No INV-1001 repeated twice
    "duplicate_ids.csv": "Txn ID,"
    + H
    + "T1,"
    + CLEAN[0]
    + "T2,2024-04-05,2024-04-01,5020,Blue Dart Express,Courier charges,INV-1001,1240.50,Dr,INR\n"
    + "T1,"
    + CLEAN[2]
    + "T4,2024-04-15,2024-04-12,5010,Acme Supplies Ltd,Desk,INV-1001,8000.00,Dr,INR\n"
    + "T5,"
    + CLEAN[4],
    # 11. balance discontinuity: debit raises the cash balance; row 4 shows 740.00 where 850 - 100 = 750;
    #     row 5 continues correctly from the stated 740 (740 + 10 = 750) → exactly 1 break, at row 4
    "balance.csv": "Date,Account,Description,Amount,Dr/Cr,Balance\n"
    "2024-04-01,1000-Cash,Opening funds,1000.00,Dr,1000.00\n"
    "2024-04-02,1000-Cash,Supplier payment,200.00,Cr,800.00\n"
    "2024-04-03,1000-Cash,Cash sale,50.00,Dr,850.00\n"
    "2024-04-04,1000-Cash,Rent,100.00,Cr,740.00\n"
    "2024-04-05,1000-Cash,Cash sale,10.00,Dr,750.00\n",
    # 12. unmapped columns: 3 extra columns
    "unmapped.csv": "Date,Invoice Date,Account,Vendor,Description,Invoice No,Amount,Dr/Cr,Currency,Cost Centre,"
    "Project,Approver\n" + "".join(r.rstrip("\n") + ",ADMIN,P-1,asha\n" for r in CLEAN),
    # 13. multiple simultaneous issues (case period 2024-04-01..2024-04-30):
    #     row 2 exact duplicate of row 1; row 3 blank amount; row 4 zero amount AND out of period AND bad date-free;
    #     row 5 unparseable date; row 6 negative amount
    "multi.csv": H
    + CLEAN[0]
    + CLEAN[0]
    + "2024-04-09,2024-04-08,4000,Customer A,Receipt against invoice,RCPT-0077,,Cr,INR\n"
    + "2024-05-15,2024-04-12,5010,Acme Supplies Ltd,Free sample,INV-1003,0.00,Dr,INR\n"
    + "yesterday,2024-04-20,5030,City Power Co,Electricity April,INV-1004,15675.25,Dr,INR\n"
    + "2024-04-25,2024-04-20,5020,Blue Dart Express,Credit note,CN-0002,-75.00,Cr,INR\n",
    # 14. normalisation-required values: double/no-break spaces, zero-width char in an identifier,
    #     full-width digits in an account, lowercase type (Type column → txn_type, case: upper)
    "normalization.csv": "Date,Account,Vendor,Description,Invoice No,Type,Amount,Dr/Cr,Currency\n"
    "2024-04-02,5010,Acme  Supplies Ltd,Office chairs,INV-1001,PAYMENT,12500.00,Dr,INR\n"
    "2024-04-05,５０２０,Blue Dart Express,Courier   charges,INV-1002,payment,1240.50,Dr,INR\n"
    "2024-04-09,4000,Customer A,Receipt,RCPT​-0077,receipt,50000.00,Cr,INR\n"
    "2024-04-15,5010,Acme Supplies Ltd,Desk,INV-1003,PAYMENT,8000.00,Dr,INR\n",
    # 15. ambiguous values needing analyst confirmation (declared %Y-%m-%d)
    "ambiguous.csv": H
    + CLEAN[0]
    + "05/04/2024,2024-04-01,5020,Blue Dart Express,Courier charges,INV-1002,1240.50,Dr,INR\n"
    + "2024/04/09,2024-04-08,4000,Customer A,Receipt against invoice,RCPT-0077,50000.00,Cr,INR\n"
    + EXTRA,
}

for name, text in files.items():
    (d / name).write_bytes(text.encode("utf-8"))
    print(name, text.count("\n") - 1, "rows")
