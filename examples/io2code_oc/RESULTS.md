# Results on IO2CodeBench (115 non-multimodal tasks)

Model `gpt-6-luna` (OpenAI Responses API, `OPENCOLLAB_MAX_OUTPUT_TOKENS=65536`), OpenCollab at
`1212d526`, IO2Code at `ef15827`, Docker image `python:3.12-slim`, one run per task and setting.
Solved = all 15 held-out tests pass.

| setting | Base (62) | Algorithm (35) | Geometry (18) | total | tokens |
|---|---|---|---|---|---|
| `--mode fixed` (benchmark protocol, 8 visible examples) | 49 | 28 | 13 | 90 / 115 (78.3%) | 2.3M |
| `--mode paper-active` (paper protocol) | 53 | 33 | 18 | 104 / 115 (90.4%) | 12.7M |
| `--random-share 0.5 --coverage-gate` | 60 | 35 | 18 | 113 / 115 (98.3%) | 18.4M |
| `--rea` (all five disciplines) | 60 | 35 | 18 | 113 / 115 (98.3%) | 30.4M |

Reading the three paper-protocol rows:

- Every failure of the plain paper protocol ended by early stop with all self-chosen inputs passing
  (Prev4/5, Sum_Last7, Alternating_Last3, Div_7, Base_4_Addition, Bit_Palindrome, both parentheses
  tasks, Extra_IsPalindrome): the agent over-fits inputs it picked itself. Those are also the tasks the
  fixed protocol solved, because the benchmark's random examples exposed the rule.
- Drawing half of every batch from the benchmark's generator plus the coverage gate fixed all of them;
  two new failures appeared (Base_5_Addition: a lookup table after the agent's own out-of-range inputs
  polluted the evidence; Majority_0_3 9/15).
- The remaining three disciplines fixed those two but lost two others (Bit_Palindrome 14/15,
  Majority_0_2 11/15) at +65% tokens: the complexity gate makes the agent patch a long spurious rule
  instead of abandoning it. The two-discipline setting is the cost-effective default.

Per-task query counts in the paper protocol are 42 at early stop (2 + 4 + 6 + 8 + 10 + 12).
A rerun of 12 tasks on `deepseek-flash` (DeepSeek-V4.1-Flash, low reasoning effort, 1.5M tokens per
task) with the two-discipline setting solved 5/12; the failures ran out of budget after 1–4 iterations.
