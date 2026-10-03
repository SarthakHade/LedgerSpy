from model import predict_transaction

# Example input (demo)
input_data = {
    "amount": 70000,
    "amount_ratio": 2.5,
    "balance_error": 1000,
    "amount_deviation": 20000,
    "velocity": 0.8,
    "odd_time": 1,
    "new_vendor_risk": 1,
    "fuzzy_flag": 0,
    "multi_receiver_flag": 1,

    "type_TRANSFER": 1,
    "type_CASH_OUT": 0,
    "type_PAYMENT": 0,
    "type_DEBIT": 0
}

result = predict_transaction(input_data)

print("\n🔍 Fraud Detection Result:")
print(result)