PYTHON ?= python

.PHONY: reset baseline tests gx dbt lineage dashboard generate check

reset:
	$(PYTHON) scripts/reset_lab.py

baseline:
	$(PYTHON) scripts/run_baseline.py

tests:
	pytest tests_public tests -q

gx:
	$(PYTHON) gx/validate_orders.py

dbt:
	$(PYTHON) scripts/sync_dbt_seeds.py
	dbt build --project-dir dbt_project --profiles-dir dbt_project

lineage:
	$(PYTHON) scripts/build_lineage.py

dashboard:
	streamlit run dashboard/app.py

generate:
	$(PYTHON) scripts/generate_data.py --rows 600 --days 42 --seed 27

# Full local verification: contracts, GX, dbt, lineage, tests.
check: reset baseline gx dbt lineage tests
