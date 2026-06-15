# Third-Party Notices

DeepBind bundles open-source software components in the desktop application.
This document lists each bundled component with its license type, the upstream
copyright holder, and any required attribution. The full text of each license
referenced below is reproduced at the end of this document under "License
Texts."

DeepBind itself is released under the **Apache License 2.0**; see the
`LICENSE` and `NOTICE` files at the project root. The licenses listed below
cover the **bundled third-party components only** — they govern those
components and do not modify or supersede the Apache 2.0 license that applies
to DeepBind itself.

This document is shipped inside the application bundle as
`Contents/Resources/THIRD-PARTY-NOTICES.md` and is also surfaced in
**Settings → Acknowledgements** at runtime, so end users can read it without
external network access. The file lives in the repository at
`docs/THIRD-PARTY-NOTICES.md` (developer-readable source-of-truth) and is
copied to `frontend/public/THIRD-PARTY-NOTICES.md` for the frontend to fetch
at runtime.

## Native Binaries

The following native binaries are bundled inside the desktop installer:

| Component | Version | License | Source |
|---|---|---|---|
| **Ollama** runtime + macOS app | 0.18.0 | MIT | <https://github.com/ollama/ollama> |
| **GGML** (inside Ollama) | tracks Ollama 0.18.0 | MIT | <https://github.com/ggerganov/ggml> |
| **llama.cpp** (inside Ollama) | tracks Ollama 0.18.0 | MIT | <https://github.com/ggerganov/llama.cpp> |
| **Apple MLX** (inside Ollama macOS build) | tracks Ollama 0.18.0 | MIT | <https://github.com/ml-explore/mlx> |
| **PDFium** (via `pypdfium2`) | bundled with `pypdfium2-5.7.1` | BSD-3-Clause / Apache-2.0 dual | <https://pdfium.googlesource.com/pdfium/> |

**PDFium attribution requirement.** Per the upstream project's binary
distribution requirement, PDFium's license text and the licenses of its
dependencies (FreeType, libjpeg-turbo, libopenjpeg, libpng, libtiff, zlib —
all permissive: BSD / MIT / zlib-style) are reproduced below in the
"License Texts" section.

## Bundled Machine-Learning Model Weights

These model weights are bundled inside the desktop installer's
`_bundled_models/` directory and loaded at runtime:

| Model | Version | License | Notes |
|---|---|---|---|
| **Snowflake Arctic-Embed-L** | latest from `snowflake/snowflake-arctic-embed-l` | Apache-2.0 | Text embedding model, 1024-dim, ~1 GB ONNX. Used by the retrieval pipeline for semantic search. Per [ADR 018](architecture/decisions/018-english-only-v1-scope.md), selected for English MTEB Retrieval quality. |
| **BAAI bge-reranker-v2-m3** | quantized as `onnx-community/bge-reranker-v2-m3-ONNX` | Apache-2.0 | Cross-encoder reranking, INT8 ONNX quantization. Used by the retrieval pipeline. |
| **xx_ent_wiki_sm** | 3.8.0 | MIT (model weights), WikiNER under CC-BY 3.0 (training data) | spaCy multilingual NER. Used by `services/entity_extraction.py`. The model weights are MIT-licensed by Explosion AI; the underlying WikiNER training data is licensed under Creative Commons Attribution 3.0. **Required attribution:** "Nothman, Joel; Ringland, Nicky; Radford, Will; Murphy, Tara; and Curran, James R. (2013). Learning multilingual named entity recognition from Wikipedia. *Artificial Intelligence*, 194, 151–175." |

GGUF chat-model weights (Qwen3 family, Granite 4 family, gpt-oss) are NOT
bundled in the installer — the user pulls them post-install via `ollama pull`.
Each chat model's license terms apply at the time of download from the user's
chosen Ollama registry. The DeepBind catalog only lists Apache-2.0 / MIT
chat-model entries (per [ADR 005](architecture/decisions/005-hardware-tiered-model-stack-and-first-run-policy.md) §A's catalog-discipline rule);
non-permissive entries were removed in the 2026-05-05 catalog cleanup (audit
finding #6).

## Python Dependencies

Bundled inside the PyInstaller sidecar (`backend/.python-local/`). All
licenses are permissive (Apache-2.0 / MIT / BSD / PSF). The full license file
of each package ships inside its `*.dist-info/` directory under
`Contents/Resources/jarvis-sidecar/...` so the receiving user has direct
access to each upstream `LICENSE` file.

| Package | Version | License | Upstream |
|---|---|---|---|
| `fastapi` | 0.128.8 | MIT | <https://github.com/fastapi/fastapi> |
| `uvicorn[standard]` | 0.39.0 | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| `pydantic-settings` | 2.11.0 | MIT | <https://github.com/pydantic/pydantic-settings> |
| `aiosqlite` | 0.22.1 | MIT | <https://github.com/omnilib/aiosqlite> |
| `keyring` | 25.7.0 | MIT | <https://github.com/jaraco/keyring> |
| `pyyaml` | 6.0.3 | MIT | <https://github.com/yaml/pyyaml> |
| `python-multipart` | 0.0.26 | Apache-2.0 | <https://github.com/Kludex/python-multipart> |
| `youtube-transcript-api` | 1.2.4 | MIT | <https://github.com/jdepoix/youtube-transcript-api> |
| `trafilatura` | 2.0.0 | Apache-2.0 | <https://github.com/adbar/trafilatura> |
| `markdownify` | 1.2.2 | MIT | <https://github.com/matthewwithanm/python-markdownify> |
| `lxml_html_clean` | 0.4.4 | BSD-3-Clause | <https://github.com/fedora-python/lxml_html_clean> |
| `pypdfium2` | 5.7.1 | Apache-2.0 / BSD-3-Clause dual | <https://github.com/pypdfium2-team/pypdfium2> |
| `defusedxml` | 0.7.1 | PSF-2.0 | <https://github.com/tiran/defusedxml> |
| `ollama` | 0.6.2 | MIT | <https://github.com/ollama/ollama-python> |
| `fastembed` | 0.8.0 | Apache-2.0 | <https://github.com/qdrant/fastembed> |
| `psutil` | 7.2.2 | BSD-3-Clause | <https://github.com/giampaolo/psutil> |
| `cryptography` | 47.0.0 | Apache-2.0 / BSD-3-Clause dual | <https://github.com/pyca/cryptography> |
| `spacy` | 3.8.14 | MIT | <https://github.com/explosion/spaCy> |
| `mcp` | 1.27.0 | MIT | <https://github.com/modelcontextprotocol/python-sdk> |

`fastembed` uses ONNX Runtime (MIT) at runtime for ONNX model inference;
ONNX Runtime is installed as a transitive dependency.

`numpy` (BSD-3) and other transitive scientific-computing dependencies of
`fastembed` and `spacy` are bundled by PyInstaller as part of the
sidecar; their license files ship inside their respective `*.dist-info/`
directories.

The Python interpreter itself (CPython 3.12) is bundled at
`backend/.python-local/` under the **Python Software Foundation License
Version 2 (PSF-2.0)**. The full PSF license text ships at
`backend/.python-local/python/LICENSE.txt`.

## Frontend Dependencies

The Vue / Nuxt renderer ships compiled into `frontend/.output/public/` and is
loaded into the Tauri WebView. Production runtime dependencies:

| Package | Version | License | Upstream |
|---|---|---|---|
| `nuxt` | 4.4.2 | MIT | <https://github.com/nuxt/nuxt> |
| `vue` | 3.5.32 | MIT | <https://github.com/vuejs/core> |
| `vue-router` | 5.0.4 | MIT | <https://github.com/vuejs/router> |
| `@nuxt/icon` | 2.1.0 | MIT | <https://github.com/nuxt/icon> |
| `@iconify-json/ph` (Phosphor Icons) | 1.2.2 | MIT | <https://github.com/phosphor-icons/core> |
| `@iconify-json/simple-icons` | 1.2.80 | CC0-1.0 (icon SVGs) / MIT (package) | <https://github.com/simple-icons/simple-icons> |
| `@tauri-apps/api` | 2.11.0 | Apache-2.0 / MIT dual | <https://github.com/tauri-apps/tauri> |
| `crossws` | 0.4.5 | MIT | <https://github.com/unjs/crossws> |
| `dompurify` | 3.4.0 | Apache-2.0 / MPL-2.0 dual | <https://github.com/cure53/DOMPurify> |
| `force-graph` | 1.51.2 | MIT | <https://github.com/vasturiano/force-graph> |
| `marked` | 18.0.0 | MIT | <https://github.com/markedjs/marked> |
| `three` | 0.183.2 | MIT | <https://github.com/mrdoob/three.js> |

Phosphor Icons are licensed under the MIT License and require attribution
when redistributed. Attribution: "Phosphor Icons by Helena Zhang and Tobias
Fried (<https://phosphoricons.com>), licensed under the MIT License."

Simple Icons SVG glyphs are released into the public domain under
**Creative Commons Zero v1.0 Universal (CC0-1.0)**; no attribution required,
but the upstream project notes are reproduced below for transparency.

## Rust Crates (Tauri Shell)

The Tauri 2 desktop shell statically links the following Rust crates into the
shipped binary. All are dual-licensed Apache-2.0 / MIT.

| Crate | License | Upstream |
|---|---|---|
| `tauri` 2.10.3 | Apache-2.0 / MIT dual | <https://github.com/tauri-apps/tauri> |
| `tauri-plugin-log` 2.x | Apache-2.0 / MIT dual | <https://github.com/tauri-apps/plugins-workspace> |
| `tauri-plugin-shell` 2.x | Apache-2.0 / MIT dual | <https://github.com/tauri-apps/plugins-workspace> |
| `tokio` 1.x | MIT | <https://github.com/tokio-rs/tokio> |
| `serde` 1.0 | Apache-2.0 / MIT dual | <https://github.com/serde-rs/serde> |
| `serde_json` 1.0 | Apache-2.0 / MIT dual | <https://github.com/serde-rs/json> |
| `log` 0.4 | Apache-2.0 / MIT dual | <https://github.com/rust-lang/log> |
| `reqwest` 0.12 | Apache-2.0 / MIT dual | <https://github.com/seanmonstar/reqwest> |

Transitive Rust dependencies pulled in by the above crates are similarly
permissive (overwhelmingly Apache-2.0 / MIT dual). The full transitive
license inventory is generated at build time by `cargo about` and shipped
alongside this document during release builds.

## Fonts and Icons

DeepBind ships **no bundled font files**. The renderer uses the operating
system's installed font stack (system-ui / SF Pro on macOS, Segoe UI on
Windows, Inter or system default on Linux). No font-license attribution
applies.

Icons are vector SVGs from Phosphor Icons (MIT) and Simple Icons (CC0-1.0)
loaded via `@nuxt/icon` from the `@iconify-json/*` offline packs bundled in
the renderer build. See "Frontend Dependencies" above for attribution.

## Components NOT Bundled

For audit clarity, the following components are explicitly **not** bundled:

- No telemetry SDKs (no Sentry, PostHog, Mixpanel, Amplitude, Segment,
  Datadog, Google Analytics, gtag).
- No cloud LLM SDKs (no OpenAI, Anthropic, Google, Mistral, etc. — per
  [ADR 015](architecture/decisions/015-single-target-local-only-stack.md)).
- No proprietary fonts, icon sets, or stock-photo libraries.
- No web-search providers (per [ADR 020](architecture/decisions/020-web-search-dropped-v1.md), v1 has no web-search affordance).
- No GPL- or LGPL-licensed code paths (the audit closed the last GPL exposure,
  `pl_core_news_sm`, on 2026-05-05; see commercial-licensing-audit.md
  finding #1).
- No non-permissively licensed model weights (Mistral Research / Gemma TOU
  entries removed 2026-05-05 per audit finding #6).

---

## License Texts

The full text of each license referenced above is reproduced in this section.

### Apache License, Version 2.0

```
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for describing the origin of the Work and
      reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Support. While redistributing the Work or
      Derivative Works thereof, You may choose to offer, and charge a
      fee for, acceptance of support, warranty, indemnity, or other
      liability obligations and/or rights consistent with this License.
      However, in accepting such obligations, You may act only on Your
      own behalf and on Your sole responsibility, not on behalf of any
      other Contributor, and only if You agree to indemnify, defend,
      and hold each Contributor harmless for any liability incurred by,
      or claims asserted against, such Contributor by reason of your
      accepting any such warranty or support.

   END OF TERMS AND CONDITIONS
```

### MIT License

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

The MIT License is reproduced once above. Each MIT-licensed component listed
in this document is governed by the same terms with the copyright holder
substituted for that component's upstream copyright owner. Per-component
copyright notices ship inside each package's installed directory (e.g.
`backend/.python-local/lib/python3.12/site-packages/fastapi-0.128.8.dist-info/LICENSE`).

### BSD 3-Clause License

```
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

### Mozilla Public License 2.0 (MPL-2.0)

The full text of MPL-2.0 is available at <https://www.mozilla.org/en-US/MPL/2.0/>
and ships inside `dompurify`'s `LICENSE` file in
`frontend/node_modules/dompurify/LICENSE`. DOMPurify is dual-licensed
Apache-2.0 / MPL-2.0; the user may select either at their choice.

### Python Software Foundation License Version 2 (PSF-2.0)

The full text of the PSF-2.0 license, governing the bundled CPython 3.12
interpreter, ships inside the bundle at
`backend/.python-local/python/LICENSE.txt` and is also available at
<https://docs.python.org/3/license.html>.

### Creative Commons Attribution 3.0 Unported (CC-BY-3.0)

The WikiNER training data underlying the bundled `xx_ent_wiki_sm` NER model
is licensed under CC-BY-3.0. Full license text:
<https://creativecommons.org/licenses/by/3.0/legalcode>. Required attribution
is reproduced in the "Bundled Machine-Learning Model Weights" section above.

### Creative Commons Zero v1.0 Universal (CC0-1.0)

The Simple Icons SVG glyphs used via `@iconify-json/simple-icons` are
released into the public domain under CC0-1.0. No attribution required;
upstream notes are reproduced for transparency. Full text:
<https://creativecommons.org/publicdomain/zero/1.0/legalcode>.

---

## Document Maintenance

This file is the single source of truth for third-party attribution. When
adding or removing a bundled dependency:

1. Update the relevant table in this document with the package name,
   version, license, and upstream URL.
2. If the new dependency has an attribution requirement (Apache-2.0, BSD,
   CC-BY, etc.), add the attribution text under the appropriate section.
3. Run `cp docs/THIRD-PARTY-NOTICES.md frontend/public/THIRD-PARTY-NOTICES.md`
   so the runtime panel picks up the change. The build pipeline also
   performs this copy as a release-time check; manual copy keeps dev builds
   in sync.
4. Bump `last_updated` in the licensing-feature registry entry
   (`docs/.registry.json`) and add a `_note_<date>` summarising the change.

The file is referenced from:

- [`backend/.python-local/python/LICENSE.txt`](../backend/.python-local/) — bundled CPython license, supplements this doc for the interpreter
- [`frontend/public/THIRD-PARTY-NOTICES.md`](../frontend/public/THIRD-PARTY-NOTICES.md) — runtime-fetched copy for the Acknowledgements panel
- [`desktop/src-tauri/tauri.conf.json`](../desktop/src-tauri/tauri.conf.json) `bundle.resources` — defense-in-depth bundle of this file inside the `.app`
- [`docs/research/commercial-licensing-audit.md`](research/commercial-licensing-audit.md) — finding #5 was closed by shipping this document
