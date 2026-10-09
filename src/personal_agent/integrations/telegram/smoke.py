"""UTF-safe Telegram smoke-test content."""

# Keep this source ASCII-only. It prevents Windows PowerShell 5 pipelines from replacing Hebrew
# with question marks before Python or Telegram receives it.
TEST_MESSAGES = (
    (
        "\u2705 \u05d1\u05d3\u05d9\u05e7\u05ea Matzpen: "
        "\u05d4\u05d7\u05d9\u05d1\u05d5\u05e8 \u05dc\u05d8\u05dc\u05d2\u05e8\u05dd "
        "\u05e2\u05d5\u05d1\u05d3 \u05d5\u05d4\u05e2\u05d1\u05e8\u05d9\u05ea "
        "\u05ea\u05e7\u05d9\u05e0\u05d4."
    ),
    (
        "\u2328\ufe0f \u05dc\u05d1\u05d3\u05d9\u05e7\u05ea \u05e9\u05e2\u05d4: "
        "\u05dc\u05d7\u05e5 \u05e2\u05dc \u05db\u05ea\u05d5\u05d1 \u05e9\u05e2\u05d4 "
        "\u05d5\u05d4\u05e9\u05d1 \u05dc\u05de\u05e9\u05dc 19:30."
    ),
    (
        "\U0001f3a4 \u05dc\u05d1\u05d3\u05d9\u05e7\u05ea \u05e7\u05d1\u05d5\u05e6\u05d4: "
        "\u05e9\u05dc\u05d7 /groups, \u05d4\u05e4\u05e2\u05dc \u05de\u05e2\u05e7\u05d1, "
        "\u05d5\u05d0\u05d6 \u05e9\u05dc\u05d7 \u05d4\u05d5\u05d3\u05e2\u05d4 "
        "\u05e7\u05d5\u05dc\u05d9\u05ea \u05d1\u05e7\u05d1\u05d5\u05e6\u05d4."
    ),
)


def validated_test_messages() -> tuple[str, ...]:
    if any("?" in message or "\ufffd" in message for message in TEST_MESSAGES):
        raise RuntimeError("Telegram smoke-test text contains encoding replacement characters")
    return TEST_MESSAGES
