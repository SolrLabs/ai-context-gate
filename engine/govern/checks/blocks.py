"""Generated blocks are fresh."""
from __future__ import annotations

from govern import blocks
from govern.findings import Findings
from govern.manifest import check
from govern.text import read


@check("generated-blocks", scope="workspace", since="0.4.0", also_project=True,
       summary="Every configured generated block exists, holds its markers, and matches what "
               "`index` would write.",
       question="Which docs carry generated index blocks?",
       rationale="The block is what an agent reads to decide which doc to open; a stale one "
                 "sends it to the wrong file or hides one.")
def generated_blocks(ctx, params, scope=None) -> Findings:
    f = Findings()
    for path, bid, build in blocks.targets(ctx, scope):
        rel = ctx.rel(path)
        if not path.exists():
            f.error(f"{rel}: generated block '{bid}' is configured but the file is missing")
            continue
        have = blocks.extract(ctx, read(path), bid)
        if have is None:
            f.error(f"{rel}: generated block '{bid}' has no markers — restore them, or remove "
                    f"the block from [blocks]")
            continue
        if have == blocks.PLACEHOLDER:
            f.warn(f"{rel}: generated block '{bid}' has never been generated")
        elif have != build().strip():
            f.error(f"{rel}: generated block '{bid}' is stale — run `index`")
    return f
