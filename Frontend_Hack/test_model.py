import pandas as pd

# 👉 Step 1: load dataset
df_test = pd.read_csv("test_transactions.csv")

# 👉 Step 2: loop through data
for i, row in df_test.iterrows():

    input_data = {
        "amount": row["amount"],
        "step": 10,

        "velocity": 1 if row["frequent"] == "Yes" else 0.1,
        "odd_time": 1 if row["time"] == "Night" else 0,
        "fuzzy_flag": 1 if row["fuzzy"] == "Yes" else 0,
        "multi_receiver_flag": 1 if row["frequent"] == "Yes" else 0,
        "new_vendor_risk": 1 if row["new_receiver"] == "Yes" else 0,

        "amount_ratio": row["amount"] / (row["sender_balance"] + 1),
        "balance_error": abs((row["sender_balance"] - row["receiver_balance"]) - row["amount"]),
        "amount_deviation": 20000
    }

    # 👉 IMPORTANT: import your model function
    from model import predict_transaction   # change file name if needed

    result = predict_transaction(input_data)

    print("\n======================")
    print(f"Transaction {i+1}")
    print("Amount:", row["amount"])
    print("Risk:", result["risk_score"])
    print("Decision:", result["decision"])
    print("Reasons:", result["reasons"])