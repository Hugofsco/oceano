"""Terminal chat with Oceano. The simplest frontend — proves the core works.

    python cli.py
"""
import config
from oceano.agent import Agent

DIM, CYAN, GREEN, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[0m"


def on_event(kind, data):
    if kind == "tool_call":
        print(f"{CYAN}  → {data['name']}({data['args']}){RESET}")
    elif kind == "tool_result":
        preview = data["result"].replace("\n", " ")[:120]
        print(f"{DIM}    {preview}{RESET}")


def main():
    print(f"Oceano — model={config.MODEL}  workspace={config.WORKSPACE}")
    print("Type a task. Ctrl-C to quit.\n")
    agent = Agent(on_event=on_event)
    while True:
        try:
            user = input(f"{GREEN}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye"); break
        if not user:
            continue
        answer = agent.run(user)
        print(f"\n{CYAN}oceano ›{RESET} {answer}\n")


if __name__ == "__main__":
    main()
