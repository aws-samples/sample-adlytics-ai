#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Refuse an AWS list or describe call in src/ or validation/ that reads only its first page.

A paginated operation returns one page and a continuation token; a caller that ignores the token
sees a truncated set with no signal. DescribeStackResources is worse: it stops at 100 resources
and has no token at all, so `adapters.outbound.cloudformation_stack.stack_resource_summaries`
(ListStackResources, every page) is the one reader of a stack's resources.

Rules, over every call in `src/**/*.py` and `validation/**/*.py`:
  1. A call to `describe_stack_resources` is refused. It cannot be justified.
  2. A direct attribute call `.list_…(` or `.describe_…(` whose name is paginatable in any
     service is refused unless it sits in a manual pagination loop or is justified. A manual
     pagination loop is a `for` or `while` loop holding an attribute call to the same method (a
     bare local function of that name does not count) that resumes at its own previous page:
     one paginator's complete continuation position (every input token, so S3
     `list_object_versions` needs both `KeyMarker` and `VersionIdMarker`) reaches that call,
     each token fed, inside the loop, from a value derived from that call's own response
       a. as a keyword whose value reads a derived name, or
       b. through `**request`, the loop assigning `request["<input token>"]` a derived value;
     the assigned keys and the call's own keywords together complete one position. A branch
     under a constant test (`if False:`) is not evidence. A sibling call is covered only as the
     first-page variant of that same chain, inside the innermost such loop: the same receiver,
     the same request but for the input tokens (it passes none), the branch of an `if` or
     conditional expression whose test reads the chain's response-derived token and is taken
     when that token is absent, and its page bound to a name the proven call's page is bound to
     (the call is the assigned value or an arm of a conditional one, not merely inside it).
     Equal source is not equal values, so no name, attribute or subscript its receiver or request
     reads may be rebound anywhere in that loop (`client = other` in one branch). So the
     first-page call of `page = c.f(...) if not token else c.f(..., NextToken=token)` is
     covered; a same-method call on another receiver, with another request, on a receiver or
     request rebound in the loop, under a test that is not the chain's token, beside the chain,
     or outside that loop is not. Otherwise
       c. it carries `# aws-pagination: <reason>` on its line, or alone on the line directly
          above (a trailing marker on the line above belongs to that line's call).
  3. `get_paginator("…")` is always compliant: it is not a list or describe call.
  4. A justification that excuses no call is stale and is a finding: one attached to no call rule
     2 would refuse, and one on a rule-1 call, which no reason excuses. So is a justification with
     no reason. Every run lists the justified sites.

"Paginatable" comes from botocore's bundled paginator models, read offline from the installed
SDK (no network, no credentials), with snake-case names from `botocore.xform_name`. The name is
matched in any service because the receiver's service is not known statically, so a name that
pages in another service (DynamoDB `describe_table` collides with Redshift Data's) needs a
justification. Justify only a call whose request names an exact target that bounds the result to
the requested set, such as `describe_stacks(StackName=…)`; page a prefix or listing call.

Accepted residuals. The gate sees direct attribute calls only: a bound method passed as a value
(`call=client.describe_x`) is not examined. Loop evidence is structural, not a proof of
termination: a manual loop that stops at a page cap or exits before the token runs out, and a
feed reachable only under a non-constant condition, are not examined. Nor is a change to a
first-page sibling's receiver or request that binds nothing inside the loop: a mutation through a
call (`request.update(…)`, `setattr`) or a write by a function defined outside the loop. Nor is a
receiver or non-token request that calls a function inside the loop: `_stable_in_loop` refuses
only a rebinding, so a sibling whose receiver is `next_client()`, or whose request passes
`Bucket=next_bucket()`, in both arms is treated as stable and covered, although each evaluation
can return a different client or bucket. A factory's result is meant to be bound once before the
loop. Review owns those.

Usage:  python3 scripts/check-aws-pagination.py
Exits non-zero on any finding.
"""

import ast
import io
import pathlib
import re
import sys
import tokenize
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import botocore.session
from botocore import xform_name
from botocore.exceptions import DataNotFoundError

ROOT = pathlib.Path(__file__).resolve().parent.parent
TREES = ("src", "validation")
REFUSED = "describe_stack_resources"
PREFIXES = ("list_", "describe_")
MARKER = re.compile(r"#\s*aws-pagination:(?P<reason>.*)$")
# A known paginated operation whose absence means the models were not read, so the gate would
# pass everything.
CONTROL = "list_stack_resources"
_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)


@dataclass
class Result:
    """Findings and justified sites for one or more files."""

    findings: list[str] = field(default_factory=list)
    justified: list[str] = field(default_factory=list)


Positions = frozenset[frozenset[str]]


def paginated_operations() -> dict[str, Positions]:
    """Map every paginatable operation's snake-case name to its paginators' continuation positions.

    A position is one paginator's complete set of input tokens. It is kept whole, never merged
    with another service's tokens or split into alternatives, because a manual loop that forwards
    part of a composite position resumes at the wrong place and silently omits results.
    """
    session = botocore.session.get_session()
    loader = session.get_component("data_loader")
    found: dict[str, set[frozenset[str]]] = {}
    for service in session.get_available_services():
        try:
            model = loader.load_service_model(service, "paginators-1")
        except DataNotFoundError:
            continue  # this service ships no paginator model: none of its operations page
        for operation, config in model["pagination"].items():
            token = config["input_token"]
            tokens = [token] if isinstance(token, str) else list(token)
            found.setdefault(xform_name(operation), set()).add(frozenset(tokens))
    if CONTROL not in found:
        raise RuntimeError(f"botocore's paginator models do not list {CONTROL}; nothing to judge")
    return {name: frozenset(positions) for name, positions in found.items()}


def _comments(text: str) -> tuple[dict[int, str], set[int]]:
    """Return each line's comment text and the lines where the comment stands alone.

    Read from tokens so strings never count. A comment stands alone when only whitespace precedes
    it, so a trailing comment, which belongs to its own line's code, can never be one.
    """
    comments: dict[int, str] = {}
    alone: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        comments[token.start[0]] = token.string
        if not token.line[: token.start[1]].strip():
            alone.add(token.start[0])
    return comments, alone


def _children(node: ast.AST, *, live: bool) -> list[ast.AST]:
    """Return a node's children; when `live`, drop the branch a constant test never takes."""
    test = getattr(node, "test", None)
    branching = isinstance(node, (ast.If, ast.While, ast.IfExp))
    if live and branching and isinstance(test, ast.Constant):
        branch = node.body if test.value else node.orelse
        return [test, *(branch if isinstance(branch, list) else [branch])]
    return list(ast.iter_child_nodes(node))


def _scope_nodes(scope: ast.AST, *, live: bool = False) -> Iterator[ast.AST]:
    """Yield the nodes under one node, excluding nested function and class bodies."""
    stack: list[ast.AST] = list(reversed(_children(scope, live=live)))
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED):
            continue
        yield node
        stack.extend(reversed(_children(node, live=live)))


def _called(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def _bound(target: ast.AST) -> set[str]:
    """Return the plain names an assignment target binds (tuple unpacking included)."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _bound(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for element in target.elts for name in _bound(element)}
    return set()


def _assignments(nodes: list[ast.AST]) -> list[tuple[list[ast.expr], ast.expr]]:
    """Return (targets, value) for every assignment form among the nodes."""
    found: list[tuple[list[ast.expr], ast.expr]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            found.append((node.targets, node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            found.append(([node.target], node.value))
        elif isinstance(node, ast.NamedExpr):
            found.append(([node.target], node.value))
    return found


def _derived(call: ast.Call, assignments: list[tuple[list[ast.expr], ast.expr]]) -> set[str]:
    """Return the names derived from the call's response among the loop's assignments.

    A name is derived when it is bound to an expression holding the call, or to an expression
    reading an already-derived name.
    """
    derived: set[str] = set()
    for targets, value in assignments:
        if any(node is call for node in ast.walk(value)):
            derived |= {name for target in targets for name in _bound(target)}
    grew = True
    while grew:
        grew = False
        for targets, value in assignments:
            bound = {name for target in targets for name in _bound(target)}
            if not bound <= derived and _names(value) & derived:
                derived |= bound
                grew = True
    return derived


def _resumes(call: ast.Call, positions: Positions, loop: list[ast.AST]) -> bool:
    """Rules 2a and 2b: the loop feeds this call one whole position from its own response.

    A value is derived from the response when it reads a name bound, in the loop's live nodes, to
    an expression holding the call, or to an expression reading an already-derived name. A token
    is fed when the call's keyword value reads a derived name (2a), or the call passes `**request`
    and the loop assigns `request["<token>"]` a value reading one (2b).
    """
    assignments = _assignments(loop)
    derived = _derived(call, assignments)
    passed = {
        keyword.arg
        for keyword in call.keywords
        if keyword.arg is not None and _names(keyword.value) & derived
    }
    requests = {
        keyword.value.id
        for keyword in call.keywords
        if keyword.arg is None and isinstance(keyword.value, ast.Name)
    }
    for targets, value in assignments:
        if not _names(value) & derived:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id in requests
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                passed.add(target.slice.value)
    return any(position <= passed for position in positions)


def _covered(scope: ast.AST, operations: Mapping[str, Positions]) -> set[int]:
    """Return the ids of calls inside a manual pagination loop for their method (rules 2a, 2b).

    Evidence comes from attribute calls only, as refusals do: a bare local function named like an
    operation is not a client call and cannot vouch for one. A call's evidence loop is the
    innermost loop that feeds it, and it covers only its own chain's other variant inside that
    loop (`_same_chain`).
    """
    loops = [node for node in _scope_nodes(scope, live=True) if isinstance(node, _LOOPS)]
    bodies = {id(loop): list(_scope_nodes(loop, live=True)) for loop in loops}
    evidence: dict[int, tuple[ast.AST, str]] = {}
    for loop in loops:
        for node in bodies[id(loop)]:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in operations
                and _resumes(node, operations[node.func.attr], bodies[id(loop)])
            ):
                # Loops are visited outer first, so a later feeding loop is an inner one.
                evidence[id(node)] = (loop, node.func.attr)
    calls = {id(node): node for nodes in bodies.values() for node in nodes}
    covered = set(evidence)
    for proven, (loop, name) in evidence.items():
        chain = _Chain(
            proven=calls[proven],
            inputs=frozenset(token for position in operations[name] for token in position),
            loop=bodies[id(loop)],
            node=loop,
        )
        covered |= {
            id(node)
            for node in chain.loop
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == name
            and _same_chain(chain, node)
        }
    return covered


def _selected(value: ast.expr) -> Iterator[ast.expr]:
    """Yield the expressions an assigned value can evaluate to: itself, or a conditional's arms.

    Containment is not selection: `(c.f(...), seed)[1]` holds the call but assigns `seed`.
    """
    if isinstance(value, ast.IfExp):
        yield from _selected(value.body)
        yield from _selected(value.orelse)
    else:
        yield value


def _responses(call: ast.Call, assignments: list[tuple[list[ast.expr], ast.expr]]) -> set[str]:
    """Return the names the call's response is assigned to: where its page lands.

    The call must be the value assigned, directly or as an arm of a conditional expression; a call
    nested inside another expression (a tuple, a subscript, an argument) is not its page.
    """
    return {
        name
        for targets, value in assignments
        if any(selected is call for selected in _selected(value))
        for target in targets
        for name in _bound(target)
    }


def _split(nodes: list[ast.AST], one: ast.AST, other: ast.AST) -> tuple[ast.expr, bool] | None:
    """Return the test of the `if` or `x if c else y` putting the two nodes in opposite branches.

    The flag says whether `one` sits in the branch taken when the test holds. None when no such
    `if` exists.
    """
    for node in nodes:
        if isinstance(node, ast.If):
            body, orelse = node.body, node.orelse
        elif isinstance(node, ast.IfExp):
            body, orelse = [node.body], [node.orelse]
        else:
            continue
        inside = {id(child) for branch in body for child in ast.walk(branch)}
        outside = {id(child) for branch in orelse for child in ast.walk(branch)}
        if id(one) in inside and id(other) in outside:
            return node.test, True
        if id(one) in outside and id(other) in inside:
            return node.test, False
    return None


def _holds_with_token(test: ast.expr, tokens: set[str]) -> bool | None:
    """Return whether the test holds exactly when a chain token is present, or None if unknown.

    Read forms: `token`, `not <form>`, and `token is [not] None`, `token [!]= None` or
    `token [!]= ""`. Any other test is not known to select between the first page and the next,
    so the caller refuses it.
    """
    if isinstance(test, ast.Name):
        return True if test.id in tokens else None
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _holds_with_token(test.operand, tokens)
        return None if inner is None else not inner
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.left, ast.Name)
        and test.left.id in tokens
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value in (None, "")
    ):
        if isinstance(test.ops[0], (ast.IsNot, ast.NotEq)):
            return True
        if isinstance(test.ops[0], (ast.Is, ast.Eq)):
            return False
    return None


def _request(call: ast.Call, tokens: frozenset[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return a call's positional arguments and non-token keywords, structurally, for comparison."""
    return (
        [ast.dump(argument) for argument in call.args],
        sorted(
            (keyword.arg or "**", ast.dump(keyword.value))
            for keyword in call.keywords
            if keyword.arg not in tokens
        ),
    )


@dataclass(frozen=True)
class _Chain:
    """One proven call's pagination chain: the call, its operation's input tokens, its loop.

    `loop` is the evidence loop's live nodes; `node` is the loop statement itself.
    """

    proven: ast.Call
    inputs: frozenset[str]
    loop: list[ast.AST]
    node: ast.AST


def _rebinds(node: ast.AST) -> tuple[set[str], set[str]]:
    """Return the names and the attribute or subscript targets (as source) the node rebinds.

    Every binding form counts: assignment and deletion targets, loop and `with` targets, an
    `except … as` name, an import, a nested `def` or `class`, `global` or `nonlocal`, and `match`
    captures. Dead branches and nested function bodies are included: refusing on them only refuses.
    """
    names: set[str] = set()
    places: set[str] = set()
    for child in ast.walk(node):
        context = getattr(child, "ctx", None)
        if isinstance(child, ast.Name) and isinstance(context, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, (ast.Attribute, ast.Subscript)) and isinstance(
            context, (ast.Store, ast.Del)
        ):
            places.add(ast.unparse(child))
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in child.names}
        elif isinstance(child, _NESTED):
            names.add(child.name)
        elif isinstance(child, (ast.Global, ast.Nonlocal)):
            names |= set(child.names)
        elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.MatchMapping) and child.rest:
            names.add(child.rest)
    return names, places


def _stable_in_loop(chain: _Chain, sibling: ast.Call) -> bool:
    """Return whether the loop rebinds nothing the sibling's receiver or non-token request reads.

    Structural equality says the two calls are written alike, not that they read the same values:
    `client = primary` in one branch and `client = secondary` in the other leave both written
    `client.f(...)`. So a name read by the receiver or request, or an attribute or subscript they
    read (`self.s3`), assigned anywhere in the evidence loop refuses the sibling outright.
    """
    read = [
        sibling.func.value if isinstance(sibling.func, ast.Attribute) else sibling.func,
        *sibling.args,
        *(keyword.value for keyword in sibling.keywords if keyword.arg not in chain.inputs),
    ]
    names = {name for expression in read for name in _names(expression)}
    places = {
        ast.unparse(child)
        for expression in read
        for child in ast.walk(expression)
        if isinstance(child, (ast.Attribute, ast.Subscript))
    }
    rebound_names, rebound_places = _rebinds(chain.node)
    return not (names & rebound_names or places & rebound_places)


def _first_page_request(chain: _Chain, sibling: ast.Call) -> bool:
    """Return whether `sibling` is the proven call's request with no input token.

    The same receiver (structurally, so `self.client` matches `self.client`), the same arguments
    but for the input tokens, of which it passes none, no `**request` that could hide one, and
    none of the values they read rebound inside the loop (`_stable_in_loop`).
    """
    proven = chain.proven
    return (
        isinstance(proven.func, ast.Attribute)
        and isinstance(sibling.func, ast.Attribute)
        and ast.dump(proven.func.value) == ast.dump(sibling.func.value)
        and not any(
            keyword.arg is None or keyword.arg in chain.inputs for keyword in sibling.keywords
        )
        and _request(proven, chain.inputs) == _request(sibling, chain.inputs)
        and _stable_in_loop(chain, sibling)
    )


def _first_page_branch(chain: _Chain, sibling: ast.Call) -> bool:
    """Return whether `sibling` runs exactly when the chain's token is absent, sharing its page.

    The two calls sit in opposite branches of one `if` or conditional expression whose test reads
    a token the proven call is fed from its own response; the proven call holds the branch where
    the token is present, `sibling` the one where it is absent, and their pages are bound to a
    shared name.
    """
    assignments = _assignments(chain.loop)
    derived = _derived(chain.proven, assignments)
    tokens = {
        name
        for keyword in chain.proven.keywords
        if keyword.arg in chain.inputs
        for name in _names(keyword.value) & derived
    }
    split = _split(chain.loop, chain.proven, sibling)
    return (
        split is not None
        and _holds_with_token(split[0], tokens) == split[1]
        and bool(_responses(chain.proven, assignments) & _responses(sibling, assignments))
    )


def _same_chain(chain: _Chain, sibling: ast.Call) -> bool:
    """Return whether `sibling` is the other variant of the proven call's own pagination chain.

    The first-page call of `page = c.f(...) if not token else c.f(..., NextToken=token)` has no
    token to feed, so it is covered by its sibling only when it is that sibling's first-page
    variant: the same request without a token (`_first_page_request`), taken exactly when the
    chain's token is absent (`_first_page_branch`). A same-named call on another receiver, with
    another request, beside the chain, under a test that is not the chain's token, or with its
    result kept apart reads its own first page and stays a finding.
    """
    return sibling is chain.proven or (
        _first_page_request(chain, sibling) and _first_page_branch(chain, sibling)
    )


def _sites(
    tree: ast.Module, operations: Mapping[str, Positions]
) -> list[tuple[ast.Call, str, bool]]:
    """Return (call, name, refused-outright) for every call rules 1 and 2 would refuse."""
    sites: list[tuple[ast.Call, str, bool]] = []
    for scope in ast.walk(tree):
        if not isinstance(scope, _SCOPES):
            continue
        calls = [node for node in _scope_nodes(scope) if isinstance(node, ast.Call)]
        covered = _covered(scope, operations)
        for call in calls:
            name = _called(call)
            if name == REFUSED:
                sites.append((call, name, True))
            elif (
                name is not None
                and isinstance(call.func, ast.Attribute)
                and name.startswith(PREFIXES)
                and name in operations
                and id(call) not in covered
            ):
                sites.append((call, name, False))
    return sorted(sites, key=lambda site: (site[0].lineno, site[0].col_offset))


def _marker_at(comments: Mapping[int, str], alone: set[int], line: int) -> tuple[int, str] | None:
    """Return the justification attached to a call line: same line, then a lone line above.

    The line above counts only when its comment stands alone; a trailing marker there justifies
    the call it trails, never the next line's call.
    """
    for at in (line, line - 1):
        if at != line and at not in alone:
            continue
        match = MARKER.search(comments.get(at, ""))
        if match is not None:
            return at, match.group("reason").strip()
    return None


def findings(rel: str, text: str, operations: Mapping[str, Positions]) -> Result:
    """Return every finding and justified site in one file's text."""
    tree = ast.parse(text)
    comments, alone = _comments(text)
    result = Result()
    # Lines of justifications that excuse a call. A rule-1 call is refused whatever its marker
    # says, so its marker is never recorded here and the stale pass below reports it.
    used: set[int] = set()
    for call, name, refused in _sites(tree, operations):
        line = call.func.end_lineno or call.lineno
        where = f"{rel}:{line}: {name}"
        if refused:
            result.findings.append(
                f"{where} stops at 100 resources with no continuation token; use "
                "adapters.outbound.cloudformation_stack.stack_resource_summaries"
            )
            continue
        marker = _marker_at(comments, alone, line)
        if marker is not None:
            used.add(marker[0])
        if marker is None:
            result.findings.append(
                f"{where} reads one page; use get_paginator, loop on its input token, or "
                "justify an exact-target call with '# aws-pagination: <reason>'"
            )
        elif not marker[1]:
            result.findings.append(
                f"{where} carries an aws-pagination justification with no reason"
            )
        else:
            result.justified.append(f"{where} — {marker[1]}")
    result.findings += [
        f"{rel}:{line}: aws-pagination justification is attached to no call it can excuse"
        for line, comment in sorted(comments.items())
        if MARKER.search(comment) is not None and line not in used
    ]
    return result


def main() -> int:
    """Check every module under src/ and validation/ and report; return the exit status."""
    operations = paginated_operations()
    total = Result()
    files = sorted(path for tree in TREES for path in (ROOT / tree).rglob("*.py"))
    for path in files:
        one = findings(str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"), operations)
        total.findings += one.findings
        total.justified += one.justified
    lines = [
        "AWS PAGINATION CHECK",
        "=" * 60,
        f"  files:       {len(files)} under {', '.join(f'{tree}/' for tree in TREES)}",
        f"  paginated:   {len(operations)} operation names from botocore's local models",
        "",
    ]
    if total.findings:
        lines.append(f"FINDINGS ({len(total.findings)})")
        lines += [f"    ✗ {finding}" for finding in total.findings]
        lines.append("")
    lines.append(f"JUSTIFIED ({len(total.justified)})")
    lines += [f"    · {site}" for site in total.justified]
    lines.append("")
    if total.findings:
        lines.append(f"{len(total.findings)} list or describe calls read one page or mis-justify.")
    else:
        lines.append(
            f"Every list or describe call pages or is exact; {len(total.justified)} justified."
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return 1 if total.findings else 0


if __name__ == "__main__":
    sys.exit(main())
