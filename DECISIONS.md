# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** `employees`: unique `emp_code` (identity, race-safe 409), `department+emp_code` (filtered list), `joined_on` and `department+joined_on` (headcount for summary and trend). `attendance_logs`: unique `emp_code+date` (one record per day, monthly, PATCH, lookups), `date desc+emp_code` (list sort, month ranges), `status+date+emp_code` (status filter), `emp_code+punch_in desc` (punch-out). I rejected a `late_minutes` index: the month range already narrows the leaderboard enough.

2. **Punch-in race.** Both requests load the employee and compute the same `date`, then both call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept exactly one; the other gets `DuplicateKeyError`, which I return as 409. There is no check-then-insert.

3. **Ties.** `$rank` gives 1, 2, 2, 4. I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that is more than `limit` rows.

4. **Headcount.** The pipeline starts from `employees` (joined by month end) and `$lookup`s their logs. Employees with no logs still appear and add 1 to headcount, with zero stats.

5. **100x data.** Pre-aggregate daily department totals when records are written, so trend and summary read small rollups instead of raw logs.
