# Original Problem Statement — "LedgerSpy: The Air-Gapped Forensic Sidekick"

Source: SE Hackathon, track **AI ML**, problem **PS 1**. The original image is
[`problem-statement.png`](problem-statement.png). Below is a faithful transcription.
This document is the authoritative statement of the *original* intent.
[`requirements.md`](requirements.md) turns it into the requirement list the project now builds against.

## Scenario

> Piyush, a young Chartered Accountant, walks into a high-stakes audit engagement with a strict
> warning from the senior partner: "One data leak and we can lose our license." With his Wi-Fi
> disabled and the pressure mounting, Piyush launches LedgerSpy on his laptop. While his teammates
> are still manually scrolling through thousands of Excel rows, LedgerSpy quietly analyzes the
> entire ledger. By lunch, Piyush uncovers a sophisticated circular trading network involving ghost
> vendors that had been draining the client's funds for over three years. The partner is stunned not
> just by the fraud, but by how a single junior auditor using only his laptop just saved the entire
> engagement.

## Main features

- **Digital Frequency Profiling.** Detects if transaction amounts are being fabricated by humans rather than
  occurring naturally. The auditor sees a clean bar chart comparing the ledger's leading digits against
  the ideal Benford's Law distribution, with clear color highlights on significant deviations.
- **Fuzzy Entity Reconciliation.** Finds hidden vendor collusion where names are slightly altered to
  bypass checks. The user gets an interactive similarity matrix showing matched pairs (e.g.,
  "Stationery Ltd" vs "Stationery L.T.D") along with shared payment patterns and risk scores.
- **Anomaly & Outlier Detection.** Highlights unusual transactions without needing labeled fraud data.
  Auditors can explore a dynamic scatter plot where red-highlighted outliers are clickable, revealing
  full transaction details and context on hover or click.
- **Bank Statement Reconciliation.** Automates vouching by matching ledger entries against scanned bank
  statements. The interface shows a clean side-by-side view with color-coded rows: green for matched,
  yellow for partial match, and red for missing evidence.
- **Relational Risk Mapping.** Reveals hidden links between employees, vendors, and transactions. The
  auditor interacts with a glowing node-link graph where node size and glow intensity reflect risk
  level and centrality, making key gatekeepers in a fraud ring instantly visible.
- **Explainable Risk Insights.** Removes the black-box problem so findings can be defended in court. For
  every flagged item, the user sees a simple breakdown chart showing which factors contributed most to
  the risk score (e.g., "65% due to unusual timing + high amount").
- **Local Audit Memo Assistant.** Turns complex analysis into professional language. With one click, the
  auditor gets a well-drafted audit summary in standard accounting tone, ready to copy into reports.
- **Data Integrity Dashboard.** Ensures the data is clean before analysis begins. The user sees a
  beautiful dashboard with a prominent "Readiness Score" percentage, along with categorized issue lists
  and visualizations for null values, duplicates, and format mismatches.

## Brownie points (original hackathon extras)

- **Monte Carlo Going Concern Stress Test.** Simulates thousands of possible cash-flow futures. The auditor
  views an intuitive probability chart showing the chance of the company surviving the next 12 months,
  with scenario bands colored from safe (green) to critical (red).
- **Privacy-Preserving Industry Benchmarking.** Lets the auditor check if the company's error or anomaly
  rate is normal for its sector.

> Note: the hackathon is over. "Brownie points" are no longer treated as scoring extras. They are
> optional, data-dependent modules in the roadmap. See [`requirements.md`](requirements.md).
