import json
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parent / "inventory.json"


def load():
    if PATH.exists():
        return json.loads(PATH.read_text())
    return {"current_position": 0.0}


def save(data):
    PATH.write_text(json.dumps(data, indent=2) + "\n")


def apply(asset, qty):
    data = load()
    pos = float(data.get("current_position", 0))
    asset = str(asset).strip().upper()
    qty = float(qty)
    if asset == "A":
        pos = pos + qty
    elif asset == "B":
        pos = pos - qty
    else:
        print("asset must be A or B")
        return
    data["current_position"] = pos
    save(data)
    print("ok  asset", asset, "qty", qty, "current_position", pos)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        apply(sys.argv[1], sys.argv[2])
    else:
        print("inventory updater  (A adds, B subtracts)")
        print("type: A 10.5   or   B 2.0    (q to quit)")
        while True:
            line = input("> ").strip()
            if line == "" or line.lower() in ("q", "quit", "exit"):
                break
            parts = line.split()
            if len(parts) < 2:
                print("need: A|B quantity")
                continue
            apply(parts[0], parts[1])
