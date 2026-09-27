# Python Notebook Challenges

The in-class FlashEats challenge notebooks from Classes 5 to 7, solved and run top to bottom with their outputs saved.

| Notebook | Class | Question it answers |
|---|---|---|
| [FlashEats_Class5_Solution.ipynb](FlashEats_Class5_Solution.ipynb) | 5, Retrieve data | How big is the late-delivery problem, and did we retrieve every record (SQL, CSV, JSON, paged API)? |
| [FlashEats_Class6_Solution.ipynb](FlashEats_Class6_Solution.ipynb) | 6, Validate | Can leadership publish "Late Delivery Rate = 56%"? (validation gate: PASS / WARN / FAIL / UNKNOWN) |
| [FlashEats_Class7_Solution.ipynb](FlashEats_Class7_Solution.ipynb) | 7, Model the workflow | Where does delay build up, and which metrics link the workflow to the KPI? |

The same habits as the main project apply: one row per business entity before counting, flag bad records instead of deleting them, report the range a number could take when data is missing, and say "associated with", never "causes".

## Run them

The FlashEats data pack belongs to the instructor and is not included here. To rerun:

1. Put the pack in `Code/` at the repo root, so that `Code/database/flasheats.db` exists.
2. `pip install -r requirements.txt flask` (Flask runs the mock dispatch API in Class 5).
3. Run Class 5 first: Class 7 reuses the raw dispatch pages that Class 5 saves.
