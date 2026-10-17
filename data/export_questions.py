import json
import sqlite3
from pathlib import Path


SOURCE_DB = Path(r"D:\WorkXM\6a17\刷题\quiz.db")
OUTPUT_FILE = Path(__file__).resolve().with_name("questions.json")


def export_questions() -> int:
    source_uri = f"{SOURCE_DB.resolve().as_uri()}?mode=ro"

    with sqlite3.connect(source_uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT id, type, stem, options, answer, explanation FROM question"
        ).fetchall()

    questions = [
        {
            "id": row[0],
            "type": row[1],
            "stem": row[2],
            "options": json.loads(row[3]) if row[3] else [],
            "answer": row[4],
            "explanation": row[5],
        }
        for row in rows
    ]

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as file:
        json.dump(questions, file, ensure_ascii=False, indent=2)

    return len(questions)


if __name__ == "__main__":
    count = export_questions()
    print(f"导出了 {count} 道题")
