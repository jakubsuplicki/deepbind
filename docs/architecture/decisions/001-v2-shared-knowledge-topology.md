# ADR 001 — V2 shared-knowledge topology

**Status:** open · under evaluation
**Date:** 2026-04-24
**Related:** [ADR 002](002-pure-local-product-shape.md)

## Context

V1 ships as per-laptop, single-user, no sync. This is settled — see §3 of the product-direction doc. V2 has to answer the team/firm case: how does a 3–15 person shop retain institutional knowledge across users without a central server they don't have the operational capacity to run.

The previously-recommended hybrid (shared office GPU box + Tailscale + per-laptop models) was dropped because it reintroduces the compliance surface the wedge is built to avoid — a new machine that needs patching, backups, access control, and someone to own it. See §2 of the product-direction doc.

The product-direction doc currently records peer-to-peer mesh (Syncthing / Automerge / Yjs shape) as the V2 architecture. **This ADR reopens that decision.** Mesh was selected without an explicit evaluation against shapes that reuse storage the firm already owns. Those shapes have a materially different engineering cost, compliance posture, and failure mode — and for most Tier-5 targets (patent boutiques, expert witnesses, mining engineering shops under 20 people) the firm already uses Dropbox Business, OneDrive/SharePoint, Box, or a NAS for client documents. The knowledge layer could live alongside those documents and inherit their trust decision.

## The reframe

The project's source-of-truth doctrine commits to:
- Markdown files in `DeepFilesAI/memory/` as the canonical store.
- SQLite is an operational index (rebuildable from Markdown).
- Graph is derived (rebuildable from Markdown).

Given that doctrine, V2 sync is **not a database-replication problem**. It is a **folder-of-Markdown-files sync problem**. Derived layers (SQLite index, graph JSON, embeddings) regenerate on each peer from the canonical Markdown.

This reframe unlocks options where the sync mechanism is not something DeepFilesAI builds but something the firm already pays for and trusts.

## Decision drivers

1. **No IT, no new machine, no new vendor review.** The V1 install story must survive into V2: one installer, no admin portal, no extra box in the office.
2. **Inherit trust decisions the firm already made.** A small firm has already audited whatever it uses for client file storage; a DeepFilesAI folder inside that storage is not a new compliance surface.
3. **Offboarding must match existing firm process.** When someone leaves, the firm's existing "revoke access to client files" procedure must also remove their access to the shared knowledge layer.
4. **Offline-first is non-negotiable.** Every peer must function without network connectivity, including shared-vault queries against a local mirror.
5. **Engineering budget is small.** A V2 that takes 12–18 months to ship correctly is a V2 that doesn't get shipped. A V2 that takes 2–4 months plus good UX work ships.
6. **The strictest deployments (ITAR, defence subs, post-*Heppner* absolutists) need a stricter mode.** Whatever the default is, a pure-mesh no-third-party-storage mode must also be reachable.

## Options under evaluation

### A. Pure mesh (CRDT, no central storage of any kind)
Every laptop is a full peer. Discovery via mDNS on LAN + Tailscale or similar overlay for remote. CRDTs (Automerge / Yjs / roll-own for the graph) handle concurrent writes. Workspace key + per-matter keys for permissions.

- **Pro:** strongest sovereignty pitch — no third party touches bytes, ever.
- **Pro:** survives any network / cloud outage; works fully offline.
- **Con:** 12–18 months of real engineering (CRDT for graph, conflict UX, key management, revocation, first-peer onboarding).
- **Con:** corporate-Wi-Fi discovery failure modes (multicast blocked, client isolation, VLANs) require a Tailscale/fallback layer.
- **Con:** permissions are crypto-gated, not server-enforced — harder to reason about.

### B. Piggyback on the firm's existing cloud storage (Dropbox / OneDrive / SharePoint / Box / iCloud Drive / Google Drive)
Shared vault lives in a folder inside the firm's existing shared storage. Their sync client handles replication; each laptop has a synced local mirror. DeepFilesAI reads/writes Markdown files and treats conflict files (`filename (Sarah's conflicted copy).md`) as first-class state to resolve in UI.

- **Pro:** zero new infrastructure. The firm already signed a DPA and already trusts the vendor.
- **Pro:** offboarding is existing process — revoke folder access.
- **Pro:** sync problem is solved by the storage vendor; no CRDT engineering.
- **Pro:** works offline — sync clients all cache locally.
- **Con:** data now touches a third party, so the "never leaves your laptop" pitch weakens to "never leaves your firm's trust boundary." For most Tier-5 targets this is the same sentence. For ITAR / defence / strict post-*Heppner* deployments it is not.
- **Con:** concurrent-write semantics are vendor-specific; some conflict-file UX work required.
- **Con:** derived files (SQLite index, embeddings binaries) should *not* go through the sync — they rebuild locally. This means a `.deepfiles-cache/` pattern outside the synced folder.

### C. Piggyback on the firm's on-prem NAS / file server (SMB share)
Same shape as B but the shared vault lives on `\\fileserver\firm-vault\` — a Synology, QNAP, or Windows file server the firm already has. Each laptop keeps a local mirror and opportunistically syncs to the share when reachable.

- **Pro:** most firm-file-servers are already where Word / PDF / drawings live; zero new trust decision.
- **Pro:** genuinely stays inside the building — satisfies the strictest interpretation of sovereignty without full mesh complexity.
- **Con:** firms without a NAS (common for solo attorneys, dispersed teams) can't use this.
- **Con:** SMB over VPN for remote workers is painful.
- **Con:** laptop-local SQLite-over-SMB is notoriously unreliable — canonical store must be Markdown (file-based), not a database on the share.

### D. Git repo as vault transport
Shared vault is a git repo. Hosted on self-hosted Gitea / Forgejo / private GitHub / a peer's machine over SSH. DeepFilesAI commits changes, pulls/pushes opportunistically, surfaces merges as conflict UX.

- **Pro:** distributed VCS is literally designed for this — every peer has full history, merges are well-defined, offline-first is native.
- **Pro:** firms that already use GitHub/Gitea for code can reuse the host.
- **Con:** non-engineer users will never see git, so the UX layer has to make it fully invisible — that's real work.
- **Con:** binary files (PDFs ingested into the vault) are a bad fit for git without LFS or equivalent.
- **Con:** requires a host somewhere — either a peer's always-on machine (reintroduces the "someone must be on" problem) or a self-hosted Gitea (reintroduces the "machine in the office" problem).

### E. Promoted-peer mesh
Mesh-shaped, but one peer is designated "primary" when online, others replicate to it. Falls back to pairwise mesh when the primary is offline.

- **Pro:** simpler conflict UX than pure mesh ("Sarah's laptop was primary while you were out; here's what changed").
- **Con:** still requires full CRDT machinery for the offline-primary case.
- **Con:** marginal complexity reduction over A; probably not worth designing as a distinct shape.

### F. Hybrid (B/C primary, A mesh as fallback)
Default shape is B or C. If the shared storage is unreachable (Dropbox outage, NAS down, remote user on a bad link), peers sync pairwise via mesh and reconcile to the shared storage when it returns.

- **Pro:** best-of-both: easy happy path + survives storage outage.
- **Con:** strictly more engineering than A or B alone.
- **Con:** three-way reconciliation (local ↔ peer ↔ shared storage) is a real conflict-resolution problem.

## Trade-offs

| Dimension | A. Mesh | B. Cloud storage | C. NAS | D. Git | F. Hybrid |
|---|---|---|---|---|---|
| New infrastructure | None | None (firm already has) | None (firm already has) | Host required | None (firm already has) |
| Sovereignty pitch | Strongest | "Firm's existing trust boundary" | Strongest-without-mesh | "Firm's git host" | Inherits primary's |
| Engineering cost | High (12–18 mo) | Low–Medium (2–4 mo) | Low–Medium | Medium | High |
| Offline behaviour | Native | Native (sync client caches) | Requires local mirror | Native | Native |
| Offboarding | Crypto key revocation | Revoke folder access | Revoke share permission | Revoke repo access | Whichever is active |
| Works for ITAR / strictest | Yes | No (third-party storage) | Yes | Depends on host | Only if A mode active |
| Works for dispersed teams | Yes (with Tailscale) | Yes | Hard (SMB over VPN) | Yes | Yes |
| Works if firm has no existing shared storage | Yes | No | No | Depends | No (falls to A) |

## Recommendation (tentative — not decided)

Ship **B (cloud-storage piggyback) as the default V2 shape**, with **A (pure mesh) as the strict-mode option** for ITAR / defence / strict-privilege deployments. Skip C, D, E, F for V2 — add them only if specific deployments require it.

Rationale:
1. B has the lowest engineering cost and the smallest compliance delta for Tier-5 targets, who are the deployments the wedge was chosen for.
2. A preserves the strongest-possible guarantee for the 5–10% of deployments that genuinely need it, without forcing that cost on everyone.
3. The two modes share the same data shape (Markdown vault + rebuildable derived layers), so a customer can migrate from B to A without a re-ingest.

**This is not committed.** The decision is blocked on:
- **Customer evidence.** Research §13 already flags that 5–10 Perth conversations are needed before committing to wedge. A subset of those conversations should ask: *"Where do client files live today? Would a folder inside that, shared the same way, be acceptable?"*
- **Vendor feasibility spike.** Dropbox Business, OneDrive/SharePoint, and Google Drive all have slightly different conflict-file semantics, API access, and selective-sync behaviour. A 1–2 day spike per vendor is needed to confirm the piggyback pattern works cleanly in each.
- **Compliance sanity check.** For the legal vertical specifically, does "shared vault inside the firm's Dropbox" survive post-*Heppner* scrutiny as cleanly as pure mesh? This is a read-the-ruling question, not an engineering question.

## Alternatives rejected outright

- **Central GPU box + Tailscale (the old hybrid).** Reintroduces a new machine that needs patching, backups, access control, and an owner — the compliance surface the wedge is built to avoid.
- **Multi-tenant vendor cloud.** A US$1M+ compliance trap pre-revenue.
- **Vendor-domain admin portal + seat management.** Breaks the "your data never leaves your trust boundary" pitch.

## What V1 must not foreclose

Regardless of which V2 shape wins, V1 must preserve these properties so V2 isn't a rewrite:

- **Canonical store is Markdown on disk, not a database.** (Already in the hard rules.)
- **Derived layers (SQLite, graph, embeddings) are deterministically rebuildable from Markdown.** (Already in the hard rules.)
- **No assumption of single-writer or single-authoritative-store in the vault schema.** Every file-touching operation should be able to land on a file that has been changed underneath it.
- **Stable content-addressed identifiers** (hashes) for notes and graph nodes rather than auto-increment IDs. Makes merge tractable later.
- **An append-only operation log** per peer, even if it only feeds local replay for V1. In V2 this log becomes the sync primitive or the audit primitive depending on shape.
- **Derived / cache files must live outside the canonical vault directory** so they don't get replicated through a piggyback sync.

## Migration path

- **V1 → V2 (B mode):** customer installs the V2 build, points it at a folder inside their existing Dropbox/OneDrive, copies their existing V1 `DeepFilesAI/memory/` contents into it. Peers point at the same folder. Derived layers rebuild on each peer. Zero data migration.
- **V2 (B mode) → V2 (A mode):** customer enables strict mode. The app stops reading/writing the piggyback folder and starts peer-sync instead. The most recent mirror on any peer becomes the new authoritative source. Re-key workspace.
- **V1 → V2 (A mode) directly:** mesh onboarding; one peer acts as seeder for the others.

## Open questions to close before committing

1. Do Tier-5 targets (patent boutiques, expert witnesses, mining shops <20 people) already have shared cloud storage they trust? Evidence-gathering question for customer conversations.
2. Does Dropbox Business / OneDrive / SharePoint's conflict-file behaviour survive real-world concurrent writes cleanly enough to ship B as a default?
3. Is there a clean identity story in B mode (who wrote this change?) without reintroducing a vendor-domain admin portal? Likely: read the storage vendor's audit log via their API as an opt-in; fall back to local signing of every write with the author's Ed25519 key.
4. Does the legal vertical's post-*Heppner* privilege story hold for B mode, or does B effectively force legal deployments onto A mode? This changes whether B or A is the "default."
5. What's the minimum viable CRDT for V2 strict mode? Files only (Automerge per file, no graph CRDT) vs. files + graph? Can the graph stay derived even in strict mode?
