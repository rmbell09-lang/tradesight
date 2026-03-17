# TRADESIGHT_SHOWHN.md — HN Quality Review
**Reviewed:** 2026-03-16 by Lucky (task 850)

---

## Overall Assessment
Post is 75% ready. Core structure is solid — personal hook, clear how-it-works, good response playbook. But 4 specific issues need fixing before submission.

---

## FLAGGED EDITS (Required)

### 1. TITLE — "AI" is imprecise and will attract pedantic criticism
Current: `Show HN: I built a self-hosted AI that evolves trading strategies overnight`

Problem: HN audience will immediately ask "what AI?" — it's a genetic/evolutionary algorithm, not ML/LLM. Using "AI" loosely invites dismissal from technical readers.

Fix option A: `Show HN: I built a self-hosted genetic algorithm that evolves trading strategies overnight`
Fix option B: `Show HN: I built a tournament-style strategy evolver that runs overnight on your own hardware`
Option B avoids jargon while being accurate and intriguing.

---

### 2. BODY — Algorithm never named
Current: Steps 1-5 describe the process using "breed" and "eliminate" but never say "genetic algorithm" or "evolutionary algorithm."

Problem: Technical HN readers will ask "what's the selection/mutation mechanism?" If you don't name it, it feels hand-wavy.

Fix: Add one line after step 5: "It's a genetic algorithm with tournament selection — the mutation logic is ~200 lines of Python."

---

### 3. P&L NUMBER — Leading with it is a liability
Current: "The best RSI Mean Reversion strategy it's evolved so far: 89.4% P&L with a 2.53 Sharpe ratio. Not a live trading guarantee..."

Problem: Even with the caveat, leading with 89.4% will trigger HN's BS detectors. You'll spend the first 30 comments defending the number instead of discussing the tech.

Fix — reframe it:
> "On a 2-year backtest, it found a config with 89.4% P&L and 2.53 Sharpe. Backtest numbers are always inflated — the value is the tournament finding *relatively* better configs, not the absolute figure. Paper trade before going live."

Move the caveat BEFORE the number, not after. Lead with the methodology, not the headline result.

---

### 4. GITHUB LINE — "not yet public" is confusing
Current: `GitHub: [not yet public — distributed as a zip via Gumroad]`

Problem: "Not yet public" implies it will be public soon. HN readers may wait for a free release instead of buying. If there's no public GitHub, don't mention GitHub.

Fix option A: Remove the GitHub line entirely. Demo mode mention is enough.
Fix option B: Create a GitHub repo with README + screenshots only (no source), link that. Gives HN something to click without giving away the product.

---

## MINOR (Optional but Recommended)

### 5. Move "MIT license" earlier
Currently buried in the response playbook. HN loves open source — mention it in the body alongside "self-hosted, no cloud dependencies."

### 6. Add hardware requirement
"Runs fine on a Mac Mini" is vague. Say: "Runs on any machine with Python 3.10+ and 4GB RAM. Tested on Mac Mini M1 and Linux."

### 7. "94 tests passing" — add context
Say "94 unit tests" or "94 integration tests." Bare "tests passing" doesn't communicate much.

---

## POST TIMING
Today is Monday March 16. Checklist says post Monday–Thursday 9–11 AM ET.
Window is open today, but edits 1-4 need to happen first. Recommend: fix today, post Tuesday March 17 9 AM ET.

---

## VERDICT
Fix edits 1-4 (especially #3 on P&L framing and #1 on title) before posting. The bones are strong — these are fixable language issues, not structural ones.
