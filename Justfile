test:
    python3 -m pytest tst -q

smoke:
    python3 scripts/smoke.py

smoke-standdown:
    python3 devtools/smoke_standdown.py

lint:
    ruff check scripts hooks tst devtools
    python3 -c "import json; json.load(open('.claude-plugin/plugin.json')); json.load(open('hooks/hooks.json'))"

run: test
