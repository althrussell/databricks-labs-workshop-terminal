"""Reviewed native question protocol; no arbitrary tool instructions."""
import re


def validate_question(value):
    if (not isinstance(value, dict) or set(value) != {"kind", "tool_use_id", "questions"}
            or value["kind"] != "claude_ask_user_question" or not isinstance(value["tool_use_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value["tool_use_id"])
            or not isinstance(value["questions"], list) or not 1 <= len(value["questions"]) <= 4):
        raise ValueError("unsupported_native_question")
    for question in value["questions"]:
        if (not isinstance(question, dict) or set(question) != {"question", "header", "multiSelect", "options"}
                or any(not isinstance(question[k], str) or not 0 < len(question[k]) <= 1500 for k in ("question", "header"))
                or type(question["multiSelect"]) is not bool or not isinstance(question["options"], list)
                or not 2 <= len(question["options"]) <= 4):
            raise ValueError("unsupported_native_question")
        for option in question["options"]:
            if (not isinstance(option, dict) or set(option) != {"label", "description"}
                    or any(not isinstance(option[k], str) or not 0 < len(option[k]) <= 1500 for k in option)):
                raise ValueError("unsupported_native_question")
    if len({q["question"] for q in value["questions"]}) != len(value["questions"]):
        raise ValueError("unsupported_native_question")
    return value
