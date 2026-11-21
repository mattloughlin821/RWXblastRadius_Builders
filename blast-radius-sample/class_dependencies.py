"""Narrow literal class settings to tests that exercise their consumers."""

import ast


def assignment(tree, domain):
    body = tree.body
    for name in domain["scope"]:
        owner = next((node for node in body if isinstance(node, ast.ClassDef) and node.name == name), None)
        if owner is None:
            return None
        body = owner.body
    return next((
        node for node in body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == domain["attribute"]
    ), None)


def literal_setting(node, attribute):
    if node is None or not isinstance(node.value, ast.Dict):
        return None
    values = ast.literal_eval(node.value)
    if any(not isinstance(key, str) for key in values):
        return None
    if attribute == "arg_types":
        valid = all(type(value) is bool for value in values.values())
    else:
        valid = all(isinstance(value, tuple) and len(value) == 4 and all(type(item) is int for item in value)
                    for value in values.values())
    return values if valid and len(values) == len(node.value.keys) else None


def setting_edit(original, updated, candidates):
    try:
        before = ast.parse(original, type_comments=True)
        after = ast.parse(updated, type_comments=True)
        serialized = ast.dump(before)
        for domain in candidates:
            old_assignment = assignment(before, domain)
            new_assignment = assignment(after, domain)
            old = literal_setting(old_assignment, domain["attribute"])
            new = literal_setting(new_assignment, domain["attribute"])
            if old is None or new is None or tuple(old) != tuple(new) or old == new:
                continue
            value = new_assignment.value
            new_assignment.value = old_assignment.value
            equal_except_setting = ast.dump(after) == serialized
            new_assignment.value = value
            if equal_except_setting:
                return domain
    except (SyntaxError, ValueError, TypeError, RecursionError):
        return None
    return None


def class_dependency_mask(path, change, original, updated, domains, impact):
    if path not in updated or change.structural or not change.touched:
        return None
    # Prefer verified syntax over the patch header's optional class name.
    candidates = [
        domain for domain in domains["domains"].get(path, ())
        if all(domain["lines"][0] <= line <= domain["lines"][1] for line in change.touched)
    ]
    if not candidates:
        return None
    domain = setting_edit(original[path], updated[path], candidates)
    if not domain:
        return None

    masks = impact["masks"]
    result = 0
    if domain["attribute"] == "arg_types":
        # Validation reads required fields for a concrete expression class.
        # Follow that class and its descendants rather than the globally shared
        # attribute name, which is also present on unrelated expressions.
        for target in domain["targets"]:
            result |= masks[impact["references"].get(target, 0)]
    else:
        # Same ordered keys means membership and truthiness checks keep their
        # results. Numeric values are consumed by the tokenizer's escape reads.
        dialects = 0
        for target in domain["targets"]:
            dialects |= masks[impact["families"].get(target, 0)]
        for filename, line in domains["escape_reads"]:
            entry = impact["files"].get(filename)
            if entry:
                result |= masks[entry["lines"].get(str(line), 0)]
        result &= dialects
    return result or None
