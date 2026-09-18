import json
import sys

data = json.load(sys.stdin)

result = {
    "question": "What is the most important clue for solving the current point?",
    "guess": "",
    "confidence": 0.1
}

print(json.dumps(result))


