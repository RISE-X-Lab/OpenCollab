"""Select bounded prerequisite checks from the supplied public requirement tree."""

import json
import re

from arc_light.completion import scenario_coverage, scenario_key
from arc_light.spec import roots


def write_public_checks(document, workspace, *, requirement_ids=None):
    by_id = {}

    def visit(node):
        identifier = str(node.get("id") or node.get("req_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", identifier) or identifier in by_id:
            raise ValueError("Requirements need unique original IDs")
        scenarios = node.get("scenarios") or []
        if not isinstance(scenarios, list) or any(not isinstance(scenario, dict) for scenario in scenarios):
            raise ValueError("Public scenarios must be an array of objects")
        for scenario in scenarios:
            steps = scenario.get("steps") or []
            if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
                raise ValueError("Scenario steps must be an array of objects")
        node = dict(node, id=identifier)
        by_id[identifier] = node
        children = node.get("children") or node.get("requirements") or []
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise ValueError("Requirement children must be objects")
        for child in children:
            visit(child)

    for node in roots(document):
        visit(node)
    nodes = list(by_id.values())
    by_name = {}
    for node in nodes:
        by_name.setdefault(node.get("name"), []).append(node)

    def named(name, default=None):
        return next(iter(by_name.get(name, [])), default or {})

    leaves = [node for node in nodes if not (node.get("children") or node.get("requirements"))]
    requested = set(requirement_ids) if requirement_ids is not None else {node["id"] for node in leaves}
    unknown = requested - {node["id"] for node in leaves}
    if unknown:
        raise ValueError("Unknown or non-atomic requirement IDs: " + ", ".join(sorted(unknown)))
    required_nodes = [node for node in leaves if node["id"] in requested]
    if not required_nodes:
        raise ValueError("The requested task contains no atomic requirements")

    def given_text(node):
        return " ".join(
            " ".join(
                step.get("content", "")
                for scenario in node.get("scenarios", [])
                for step in scenario.get("steps", [])
                if step.get("keyword") == "GIVEN"
            ).split()
        )

    def sources(*selected):
        return list(dict.fromkeys(node["id"] for node in selected if node.get("id")))

    def named_account(node, scenario=None):
        # Credentials and role must belong to the SAME selected scenario.
        scenario = scenario if scenario is not None else next(iter(node.get("scenarios") or []), {})
        text = " ".join(
            str(step.get("content", "")) for step in scenario.get("steps", []) if step.get("keyword") == "GIVEN"
        )
        text = " ".join(text.split())
        match = re.search(
            r"account\s+(?:with\s+username\s+)?`([^`]+)`,?\s*(?:with\s+)?email\s+`([^`]+)`\s*,?\s*(?:and\s+)?password\s+`([^`]+)`",
            text,
            re.I,
        )
        if not match:
            match = re.search(r"account\s+`([^`]+)`\s*\(`([^`]+)`,\s*`([^`]+)`\)", text, re.I)
        if match:
            return dict(zip(("username", "email", "password"), match.groups()))
        username = re.search(r"(?:username|account|pre-provisions)\s+`([^`]+)`", text, re.I)
        email = re.search(r"email\s+`([^`]+)`", text, re.I)
        password = re.search(r"password\s+`([^`]+)`", text, re.I)
        credentials = re.search(r"credentials\s+`([^`]+)`\s+and\s+`([^`]+)`", text, re.I)
        if username and credentials:
            return {"username": username.group(1), "email": credentials.group(1), "password": credentials.group(2)}
        return (
            {"username": username.group(1), "email": email.group(1), "password": password.group(1)}
            if all((username, email, password))
            else None
        )

    descriptions = "\n".join(str(node.get("description", "")) for node in nodes)
    config = {"kind": "none", "evidence_kind": "public_prerequisites_not_full_acceptance"}
    create = named("Create a Blank Workbook", {})
    edit = named("Edit a Cell Through the Grid or Formula Bar", {})
    # Do not impose GitHub names or account assumptions on other tasks.
    markers = ("Username or email", "Agree to the terms", "Send reset link", "Invalid credentials")
    auth_checks = all(marker in descriptions for marker in markers)
    sign_in = named("Sign In with an Existing Account", {})
    search = named("Search for and Locate Repositories", {})
    organization = named("Browse Organization Repositories", {})
    issues = named("List and Filter Repository Issues", {})
    pulls = named("List and Filter Repository Pull Requests", {})
    sheet_evolution = any(
        name in by_name
        for name in (
            "Rename a Workbook",
            "Freeze Rows and Columns",
            "Find and Replace Cell Text",
            "Create and Edit Named Ranges",
            "Create, Edit, and Delete Conditional Formatting Rules",
            "Create, Edit, and Delete a Cell Note",
        )
    )
    if create or sheet_evolution or (edit and "Modified Feature Description" in descriptions):
        config.update(
            kind="sheet",
            sheet_create=bool(create),
            sheet_edit=bool(edit),
            source_ids=[node["id"] for node in (create, edit) if node.get("id")],
        )
        contracts = {}
        for key, marker in (
            ("worksheet_options", "Worksheet options for"),
            ("row_headers", "ARIA rowheader"),
            ("column_headers", "ARIA columnheader"),
            ("inline_edit", "Edit <cell coordinate>"),
        ):
            owner = next((node for node in nodes if marker in str(node.get("description", ""))), {})
            if owner:
                contracts[key] = sources(create, owner)
        copy = named("Copy, Cut, and Paste Cell Ranges", {})
        undo = named("Undo and Redo Recent Operations", {})
        formula = named("Copy Formulas and Adjust Relative References", {})
        if copy and edit:
            contracts["range_clipboard"] = sources(create, edit, copy)
            if undo:
                contracts["undo_redo"] = sources(undo)
            if formula:
                contracts["formula_copy"] = sources(formula)
        config["sheet_contracts"] = contracts
    elif (
        auth_checks
        or any((sign_in, search, organization, issues, pulls))
        or any(
            name in by_name
            for name in (
                "Manage Active Browser Sessions",
                "View Organization Audit Log",
                "Archive and Restore a Repository",
                "Create and View Repository Releases",
                "Add and Remove Issue Reactions",
                "List and Switch Repository Branches",
            )
        )
    ):
        config["kind"] = "github"
        config["auth_checks"] = auth_checks
        config["main_sign_in"] = any(
            "main content" in str(step.get("content", "")).lower()
            for scenario in sign_in.get("scenarios") or []
            for step in scenario.get("steps") or []
        )
        account = re.search(
            r"account\s+(?:with\s+username\s+)?`([^`]+)`,\s*email\s+`([^`]+)`,\s*(?:and\s+)?password\s+`([^`]+)`",
            given_text(sign_in),
            re.I,
        )
        repository = re.search(r"public repository `([^`]+)`", given_text(search))
        if named_account(sign_in):
            config["account"] = named_account(sign_in)
        elif account:
            config["account"] = dict(zip(("username", "email", "password"), account.groups()))
        if repository:
            config["public_repository"] = repository.group(1)
        organization_name = re.search(r"organization\s+`([^`]+)`", given_text(organization), re.I)
        config["public_navigation"] = [organization_name.group(1)] if organization_name else []
        for node, label, key, pattern in (
            (issues, "Issues", "issue_title", r"\bOpen\s+issue\s+(?:titled\s+)?`([^`]+)`"),
            (pulls, "Pull requests", "pull_title", r"\bOpen\s+(?:PR|pull request)\s+(?:titled\s+)?`([^`]+)`"),
        ):
            if node:
                config["public_navigation"].append(label)
                title = re.search(pattern, given_text(node), re.I)
                if title:
                    config[key] = title.group(1)
        expected = ["account", "public_repository"] if auth_checks else []
        if search and "public_repository" not in expected:
            expected.append("public_repository")
        if organization and not organization_name:
            expected.append("organization")
        expected.extend(key for node, key in ((issues, "issue_title"), (pulls, "pull_title")) if node)
        config["missing_inputs"] = [key for key in expected if key not in config]
        config["source_ids"] = [node["id"] for node in (sign_in, search, organization, issues, pulls) if node.get("id")]
        registration = named("Register a New GitHub Account", {})
        recovery = named("Recover Account Access Through a Verified Email", {})
        config["check_sources"] = {
            "registration_validation_and_persisted_signin": sources(registration, sign_in),
            "seeded_username_signin_and_reload": sources(sign_in),
            "seeded_email_signin_and_reload": sources(sign_in),
            "local_password_recovery_contract": sources(recovery),
            "public_repository_search_open_and_reload": sources(search),
            **{
                "public_navigation_from_home: " + label: sources(node)
                for label, node in (
                    (organization_name.group(1) if organization_name else "", organization),
                    ("Issues", issues),
                    ("Pull requests", pulls),
                )
                if node and label
            },
        }
        # Bounded representative HOME paths, not a second copy of the specification.
        # List-flow fixtures are not direct home links; select detail cards separately.
        targets = []
        groups = (
            ("repository", nodes, r"\bpublic repository\s+`([^`]+)`", 3),
            (
                "issue",
                [named("View an Issue and Its Discussion", {})],
                r"\b(?:Open\s+)?issue\s+(?:titled\s+)?`([^`]+)`",
                1,
            ),
            (
                "pull",
                [
                    named(name, {})
                    for name in ("View Pull Request Overview and Commits", "Inspect Changed Files and Aggregate Diff")
                ],
                r"\b(?:Open\s+)?(?:PR|pull request)\s+(?:titled\s+)?`([^`]+)`",
                2,
            ),
        )
        for kind, selected, pattern, limit in groups:
            found = {}
            for node in selected:
                text = given_text(node)
                if "unauthenticated" not in text.lower():
                    continue
                if kind == "repository" and not re.search(r"(?:application\s+)?home page", text, re.I):
                    continue
                for label in re.findall(pattern, text, re.I):
                    found.setdefault(label, []).extend(sources(node))
            for label, ids in list(found.items())[:limit]:
                targets.append(dict(kind=kind, label=label, source_ids=list(dict.fromkeys(ids))))
            if len(found) > limit:
                config.setdefault("deferred_home_targets", []).extend(list(found)[limit:])
        config["home_targets"] = targets
        menu = named("Create an Organization After Authentication", {})
        if menu:
            config["account_menu"] = {
                "account": named_account(menu) or config.get("account"),
                "source_ids": sources(menu),
            }
        compare = named("Create a Pull Request from Comparison Results", {})
        if compare and "`Compare`" in given_text(compare):
            config["compare_entry"] = {"account": named_account(compare), "source_ids": sources(compare)}
    # One representative public scenario per changed feature. No embedded seed values.
    evolution_names = {
        "sheet": (
            "Rename a Workbook",
            "Rename a Worksheet",
            "Edit a Cell Through the Grid or Formula Bar",
            "Filter Rows by Value or Condition",
            "Set Dropdown or Numeric Validation for a Range",
            "Freeze Rows and Columns",
            "Find and Replace Cell Text",
            "Create and Edit Named Ranges",
            "Create, Edit, and Delete Conditional Formatting Rules",
            "Create, Edit, and Delete a Cell Note",
        ),
        "github": (
            "Register a New GitHub Account",
            "Sign In with an Existing Account",
            "Create an Organization After Authentication",
            "Search for and Locate Repositories",
            "List and Switch Repository Branches",
            "Manage Active Browser Sessions",
            "View Organization Audit Log",
            "Archive and Restore a Repository",
            "Create and View Repository Releases",
            "Add and Remove Issue Reactions",
        ),
    }
    selected = []
    # Resolve only an explicitly referenced identity, never an arbitrary later account.
    accounts = {}
    for node in nodes:
        for scenario in node.get("scenarios") or []:
            account = named_account(node, scenario)
            if account:
                accounts.setdefault(account["username"], []).append(account)
    for node in required_nodes:
        name = node.get("name")
        if name not in evolution_names.get(config["kind"], ()):
            continue
        scenarios = node.get("scenarios") or []
        if not scenarios:
            continue
        chosen = list(range(len(scenarios)))
        for number in chosen:
            scenario = scenarios[number]
            steps = scenario.get("steps") or []
            given = " ".join(str(s.get("content", "")) for s in steps if s.get("keyword") == "GIVEN")
            account = named_account(node, scenario)
            if account is None:
                referenced = [username for username in accounts if f"`{username}`" in given]
                if len(referenced) == 1:
                    candidates = accounts[referenced[0]]
                    if all(candidate == candidates[0] for candidate in candidates):
                        account = candidates[0]
            selected.append(
                {
                    "name": name,
                    "requirement_id": node["id"],
                    "source_ids": sources(node),
                    "account": account,
                    "scenario_index": number,
                    "scenario_id": scenario_key({"requirement_id": node["id"], "scenario_index": number}),
                    "visitor": account is None
                    and bool(
                        re.search(r"visitor.*?unauthenticated|unauthenticated.*?(?:visitor|session)", given, re.I)
                    ),
                    **{
                        key.lower(): " ".join(str(s.get("content", "")) for s in steps if s.get("keyword") == key)
                        for key in ("GIVEN", "WHEN", "THEN")
                    },
                    "remaining_scenarios": max(0, len(scenarios) - len(chosen)) if number == 0 else 0,
                }
            )
    config["evolution_checks"] = selected
    if config["kind"] == "github":
        repo_item = next((item for item in selected if item["name"] == "List and Switch Repository Branches"), {})
        repo = re.search(r"public repository `([^`]+)`", repo_item.get("given", ""))
        menu = config.get("account_menu") or {}
        config["shared_inputs"] = {
            "account": config.get("account"),
            "menu_account": menu.get("account"),
            "repository": repo.group(1) if repo else None,
            "menu_ids": menu.get("source_ids", []),
            "repository_ids": repo_item.get("source_ids", []),
            "sign_in_ids": sources(sign_in),
        }
    else:
        config["shared_inputs"] = {
            "workbook": next(
                (
                    re.search(r"workbook `([^`]+)`", item["given"]).group(1)
                    for item in selected
                    if re.search(r"workbook `([^`]+)`", item["given"])
                ),
                None,
            )
        }
    from .compatibility import enrich_contract

    config = enrich_contract(config)
    mapped = {item["scenario_id"] for item in selected}
    required = []
    without_scenarios = []
    for node in required_nodes:
        scenarios = node.get("scenarios") or []
        if not isinstance(scenarios, list):
            raise ValueError("Public scenarios must be an array")
        if not scenarios:
            without_scenarios.append(node["id"])
        for index, scenario in enumerate(scenarios):
            if not isinstance(scenario, dict):
                raise ValueError("Each public scenario must be an object")
            item = {
                "name": node.get("name") or node["id"],
                "requirement_id": node["id"],
                "source_ids": [node["id"]],
                "scenario_index": index,
            }
            item["scenario_id"] = scenario_key(item)
            item["mapped"] = item["scenario_id"] in mapped
            required.append(item)
    config["persistence_requirement_ids"] = [
        node["id"]
        for node in required_nodes
        if re.search(r"\b(?:persist\w*|reload|refresh|restart)\b", str(node), re.I)
    ]
    config["requires_persistence"] = bool(config["persistence_requirement_ids"])
    config.update(
        required_requirement_ids=[node["id"] for node in required_nodes],
        required_scenarios=required,
        requirements_without_scenarios=without_scenarios,
        expected_scenarios=len(required),
        mapped_scenarios=len(selected),
    )
    path = workspace / ".arc/checks/public-prerequisites.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (path.parent / "evaluation-observations.json").write_text(
        json.dumps(
            {
                "kind": "user_supplied_evaluation_observations_not_public_spec",
                "observations": config["evaluation_observations"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (path.parent / "scenario-ledger.json").write_text(
        json.dumps(
            scenario_coverage(required, [], requirements_without_scenarios=without_scenarios),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return config
