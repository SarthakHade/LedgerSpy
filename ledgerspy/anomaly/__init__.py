"""P8 statistical anomaly detection: Isolation Forest, robust peer-group z-scores and tail percentiles.

Every detector reads P6 registry features (the feature matrix) only — never labels, ground truth, `extra` or
P7 signals — and emits `model_anomaly` signals for P9. "Anomalous" means unusual relative to a stated
population, never "fraudulent". See docs/anomaly-model-card.md.
"""
