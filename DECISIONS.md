# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Unique `employees.emp_code` protects identity and creation. `department, joined_on` and standalone `joined_on` support headcount filters. Unique attendance `emp_code, date` protects records and supports employee/month lookups. `emp_code, punch_in desc` supports latest-open-record searches; `date desc, emp_code asc` serves attendance pages; `date, late_minutes, emp_code` serves leaderboard scans. I rejected a standalone `status` index because it is low-selectivity and date/employee filters drive attendance queries.
2. **Punch-in race.** Both requests may insert, but the unique (`emp_code`, `date`) index commits only one. The losing `DuplicateKeyError` becomes 409; correctness does not depend on a check-then-insert.
3. **Ties.** The aggregation applies `$rank` using total late minutes, then filters `rank <= limit`. Ties at the limit are all returned with shared ranks and the next rank is skipped. Employee code provides stable tie ordering.
4. **Headcount.** Summary starts from eligible employees and looks up logs, so employees without logs still contribute to headcount.
5. **100x data.** I would measure the tradeoff of pre-aggregated daily/department metrics, then consider date-based partitioning or archiving.
