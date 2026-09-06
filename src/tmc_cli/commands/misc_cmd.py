"""Introspection and escape hatches: `schema`, `template`, `raw`, `completion`.

`raw` matters more than it looks. Everything else in this CLI encodes what the
API looked like when it was written, and a mirror is always one deploy behind —
`raw` is the promise that a field or an endpoint added upstream is reachable
today, without waiting for this tool to catch up.
"""

from __future__ import annotations

import json
from typing import Any

from ..context import Context
from ..errors import UsageError
from ..params import build_payload
from ..schema import (
    ANON_APP_FILTER_TYPES,
    ANON_LIST_LIMIT_DEFAULT,
    ANON_TYPES,
    ENUMS,
    MAX_BULK_DELETE,
    MAX_BULK_WRITE,
    MAX_RELATION_KEYS,
    MAX_RELATION_MEMBERS,
    MAX_UPLOAD_PARTS,
    RELATIONS,
    TYPES,
    relations_for,
)
from .relation_cmd import dump_member_template


def schema(ctx: Context) -> int:
    args = ctx.args

    if args.type:
        spec = TYPES.get(args.type)

        if spec is None:
            raise UsageError(
                f"Unknown content type '{args.type}'.",
                hint=f"Known: {', '.join(TYPES)}",
            )

        if args.output == "json":
            payload = {
                "type": spec.name,
                "canonical": spec.canonical,
                "required": list(spec.required),
                "staffOnlyWrite": spec.staff_only_write,
                "relations": relations_for(spec.name),
                "listFilters": list(spec.list_filters),
                "anonymousRead": spec.name in ANON_TYPES,
                "fields": [
                    {
                        "name": field.name,
                        "type": field.type,
                        "note": field.note,
                        "staffOnly": field.staff_only,
                        "enum": list(ENUMS[field.enum]) if field.enum else None,
                    }
                    for field in spec.fields
                ],
            }

            print(json.dumps(payload, indent=2))

            return 0

        _print_type(spec)

        return 0

    if args.output == "json":
        print(
            json.dumps(
                {
                    "types": {
                        name: {
                            "canonical": spec.canonical,
                            "required": list(spec.required),
                            "relations": relations_for(name),
                            "fields": spec.field_names(),
                        }
                        for name, spec in TYPES.items()
                    },
                    "relations": {
                        name: {
                            "parents": list(rel.parents),
                            "identity": rel.key,
                            "member": rel.member,
                        }
                        for name, rel in RELATIONS.items()
                    },
                    "limits": {
                        "bulkWrite": MAX_BULK_WRITE,
                        "bulkDelete": MAX_BULK_DELETE,
                        "relationMembers": MAX_RELATION_MEMBERS,
                        "relationDeleteKeys": MAX_RELATION_KEYS,
                        "uploadParts": MAX_UPLOAD_PARTS,
                    },
                    "anonymous": {
                        "types": list(ANON_TYPES),
                        "appFilterTypes": list(ANON_APP_FILTER_TYPES),
                        "listLimitDefault": ANON_LIST_LIMIT_DEFAULT,
                    },
                },
                indent=2,
            )
        )

        return 0

    print("Content types")
    print()

    for name, spec in TYPES.items():
        flags = []

        if spec.canonical:
            flags.append("canonical writer")

        if spec.staff_only_write:
            flags.append("staff-only write")

        rels = relations_for(name)

        if rels:
            flags.append("relations: " + ", ".join(rels))

        required = ", ".join(spec.required) or "—"

        print(f"  {name:<11} required on create: {required}")

        if flags:
            print(f"  {'':<11} {' · '.join(flags)}")

    print()
    print("Relations")
    print()

    for name, rel in RELATIONS.items():
        print(f"  {name:<9} on {', '.join(rel.parents)}")
        print(f"  {'':<9} member: {rel.member}")
        print(f"  {'':<9} identity: {rel.key}")

    print()
    print("Server-enforced limits")
    print(f"  bulk create/update   {MAX_BULK_WRITE} per request (batched automatically)")
    print(f"  bulk delete          {MAX_BULK_DELETE} ids per request")
    print(f"  relation members     {MAX_RELATION_MEMBERS} per PUT/POST")
    print(f"  relation delete keys {MAX_RELATION_KEYS} per DELETE (batched automatically)")
    print(f"  upload parts         {MAX_UPLOAD_PARTS} files per request")
    print()
    print("Without a key (--anon)")
    print(f"  readable types       {', '.join(ANON_TYPES)}")
    print("  what you get         a public summary, not the record; no relations")
    print(f"  list filters         appId only, on {', '.join(ANON_APP_FILTER_TYPES)}")
    print(f"  page size            {ANON_LIST_LIMIT_DEFAULT} by default and as the cap")
    print()
    print("Run 'tmc schema <type>' for one type's fields.")

    return 0


def _print_type(spec: Any) -> None:
    print(f"{spec.name}")

    if spec.note:
        print(f"  {spec.note}")

    print()
    print(f"  required on create: {', '.join(spec.required) or '—'}")
    print(f"  relations:          {', '.join(relations_for(spec.name)) or 'none'}")
    print(f"  list filters:       {', '.join(spec.list_filters) or 'none'}")
    print(
        "  without a key:      "
        + ("summary reads (--anon)" if spec.name in ANON_TYPES else "no")
    )
    print()
    print("  field                 type      notes")
    print("  " + "─" * 68)

    for field in spec.fields:
        notes = []

        if field.enum:
            notes.append("one of " + "|".join(ENUMS[field.enum]))

        if field.note:
            notes.append(field.note)

        if field.staff_only:
            notes.append("STAFF ONLY")

        print(f"  {field.name:<21} {field.type:<9} {'; '.join(notes)}")

    print()
    print("  Set fields with --set name=value, --set-json name='<json>' or --set-file name=path.")


def template(ctx: Context) -> int:
    relation = ctx.args.relation

    if relation not in RELATIONS:
        raise UsageError(
            f"Unknown relation '{relation}'.",
            hint=f"Known: {', '.join(RELATIONS)}",
        )

    print(dump_member_template(relation))

    return 0


def raw(ctx: Context) -> int:
    """Send an arbitrary request with the profile's credentials attached."""

    args = ctx.args

    params: dict[str, Any] = {}

    for assignment in args.param or []:
        if "=" not in assignment:
            raise UsageError(f"--param expects key=value, got '{assignment}'.")

        key, _, value = assignment.partition("=")
        params[key] = value

    body = None

    if args.json or args.set or args.set_json or args.set_file:
        body = build_payload(
            None,
            sets=args.set,
            set_jsons=args.set_json,
            set_files=args.set_file,
            json_body=args.json,
            allow_unknown=True,
        )

        # A bare `--json '[...]'` is an array, and build_payload insists on an
        # object — so an array body goes through untouched.
        if args.json and not (args.set or args.set_json or args.set_file):
            from ..params import parse_json_arg

            body = parse_json_arg(args.json)

    response = ctx.client.http.request(
        args.method.upper(), args.path, params=params, json_body=body
    )

    ctx.emit(response.body if args.envelope else response.data)

    return 0


BASH_COMPLETION = r"""
# tmc bash completion — source this file, or install it as
#   tmc completion bash > /etc/bash_completion.d/tmc
_tmc_complete() {
    local cur prev words
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local commands="__COMMANDS__"
    local types="__TYPES__"
    local relations="__RELATIONS__"

    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        return
    fi

    case "$prev" in
        rel|tags|media|links) COMPREPLY=( $(compgen -W "$types" -- "$cur") ); return ;;
        --relation)          COMPREPLY=( $(compgen -W "$relations" -- "$cur") ); return ;;
        -o|--output)         COMPREPLY=( $(compgen -W "table json jsonl csv tsv ids yaml" -- "$cur") ); return ;;
        --profile)           COMPREPLY=( $(compgen -W "$(tmc auth list -o ids 2>/dev/null)" -- "$cur") ); return ;;
    esac

    COMPREPLY=( $(compgen -W "list get create update delete --help" -- "$cur") )
}
complete -F _tmc_complete tmc
"""

ZSH_COMPLETION = r"""
#compdef tmc
# tmc zsh completion — install as _tmc on your $fpath:
#   tmc completion zsh > "${fpath[1]}/_tmc"
_tmc() {
    local -a commands types relations
    commands=(__COMMANDS__)
    types=(__TYPES__)
    relations=(__RELATIONS__)

    _arguments -C \
        '1:command:->command' \
        '2:argument:->argument' \
        '*::options:->options'

    case $state in
        command) _describe 'command' commands ;;
        argument)
            case $words[2] in
                rel|tags|media|links) _describe 'type' types ;;
                schema)               _describe 'type' types ;;
                template)             _describe 'relation' relations ;;
                *)                    _values 'subcommand' list get create update delete ;;
            esac
            ;;
    esac
}
_tmc "$@"
"""

FISH_COMPLETION = r"""
# tmc fish completion — install as ~/.config/fish/completions/tmc.fish
complete -c tmc -f
for cmd in __COMMANDS__
    complete -c tmc -n "__fish_use_subcommand" -a $cmd
end
complete -c tmc -n "__fish_seen_subcommand_from rel tags media links schema" -a "__TYPES__"
complete -c tmc -s o -l output -a "table json jsonl csv tsv ids yaml"
"""


def completion(ctx: Context) -> int:
    from ..cli import top_level_commands

    shell = ctx.args.shell

    body = {
        "bash": BASH_COMPLETION,
        "zsh": ZSH_COMPLETION,
        "fish": FISH_COMPLETION,
    }[shell]

    print(
        body.replace("__COMMANDS__", " ".join(top_level_commands()))
        .replace("__TYPES__", " ".join(TYPES))
        .replace("__RELATIONS__", " ".join(RELATIONS))
        .strip()
    )

    return 0
