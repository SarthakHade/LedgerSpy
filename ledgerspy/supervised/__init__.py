"""P10 supervised training and leakage-safe evaluation (an optional, experimental evidence layer).

Labels enter only through `ledgerspy.supervised.labels` and only into this package. P6 features, P7 rules,
P8 anomaly detection and the P9 risk score never see them and never change because an experiment ran. Model
outputs are ranking scores unless calibration is validated. See docs/supervised-model-card.md.
"""
