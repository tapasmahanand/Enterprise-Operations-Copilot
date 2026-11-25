# Kestrel Resolution Copilot: common tasks. Needs Docker; eval needs OPENAI_API_KEY in .env.
.PHONY: up down test test-db eval eval-quick compliance-eval retrieval-ablation demo workflows

up:            ## build and start everything (postgres, ingest, app, worker, Langfuse)
	docker compose up --build -d --wait app

down:
	docker compose down

test:          ## unit tests (no database, no key)
	pytest -q

test-db:       ## all tests incl. database ones (needs Postgres on localhost:5432)
	pytest -q -m "db or not db"

eval:          ## full evaluation -> reports/eval.md (real models; ~25 min, about ₹150 at list prices)
	python -m evals.run_eval

eval-quick:    ## production config only, 1 repeat, no routing sample (~3 min)
	python -m evals.run_eval --configs prod --repeats 1 --routing-n 0

compliance-eval:  ## compliance-scan precision/recall on the labelled sample (no key needed)
	python -m evals.compliance_eval

retrieval-ablation:
	python -m evals.retrieval_ablation

demo:          ## scripted hero demo with a hard app restart mid-approval
	python scripts/hero_demo.py --restart "docker compose kill app && docker compose up -d --wait app"

workflows:     ## all five workflows against the running stack
	python scripts/workflows_e2e.py
