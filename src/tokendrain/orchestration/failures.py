"""User-facing execution failures; diagnoses never infer an OOM from a lost socket."""


def execution_failure(error: Exception, *, stage: str, memory_mib: int) -> dict[str, str]:
    if isinstance(error, (ConnectionError, EOFError, TimeoutError)):
        if stage == "guest":
            title = "Connection to the project VM was lost"
            explanation = (
                "Tokendrain could no longer reach the guest supervisor. The VM or its supervisor "
                "may have stopped, restarted or run out of memory."
            )
            action = (
                f"This VM was configured with {memory_mib} MiB RAM. "
                + (
                    "That is very little for Codex and development tools. Increase Memory in "
                    "Settings → Execution defaults to 4096 MiB before starting another Run. "
                    if memory_mib < 2048
                    else "Check the VM logs for a shutdown or out-of-memory message "
                    "before retrying. "
                )
                + "A connection error alone does not confirm the cause."
            )
        else:
            title = "Execution services could not be reached"
            explanation = "Tokendrain could not reach a service needed to prepare the project VM."
            action = "Check tokendrain doctor and the daemon/helper logs, then try another Run."
    else:
        title = "The execution could not continue"
        explanation = "An execution service failed. The technical details below identify the error."
        action = "Check the execution logs before retrying."
    return {
        "title": title,
        "explanation": explanation,
        "recovery": action,
        "workspace": (
            "Tokendrain keeps the persistent project machine. Files already written "
            "remain there; interrupted commands may need to be run again. No reset is required."
        ),
    }
