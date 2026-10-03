def smart_chatbot(query, result):
    query = query.lower()

    if "why" in query or "explain" in query:
        reasons_text = "\n- ".join(result["reasons"])

        return f"""
This transaction is classified as {result['decision']}.

Risk Score: {result['risk_score']}%

Reasons:
- {reasons_text}

Recommended Action: {result['action']}
"""

    elif "risk" in query:
        return f"The risk score is {result['risk_score']}%."

    elif "action" in query:
        return f"Recommended action: {result['action']}."

    elif "fraud" in query or "safe" in query:
        return f"Transaction is: {result['decision']}."

    else:
        return """
You can ask:
- Why is this fraud?
- What is risk score?
- What action should be taken?
"""