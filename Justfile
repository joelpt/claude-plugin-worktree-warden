test:
    python3 -m pytest tst -q

lint:
    ruff check scripts hooks tst
    python3 -c "import json; json.load(open('.claude-plugin/plugin.json')); json.load(open('hooks/hooks.json'))"

run: test
