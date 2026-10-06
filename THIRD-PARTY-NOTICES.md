# Third-party notices

Dojo uses work by others. This file lists it, with its license. Dojo's own license is the [MIT License](LICENSE).

If you think something is missing or wrong here, please open an issue.

## Fonts

The fonts are bundled in [`app/static/fonts`](app/static/fonts) as WOFF2 files (Latin subsets). They are
licensed under the **SIL Open Font License, Version 1.1**. The full license text is next to each font.

| Font | Files | Copyright | License |
|---|---|---|---|
| Fraunces | `Fraunces-normal.woff2`, `Fraunces-italic.woff2` | Copyright 2020 The Fraunces Project Authors (github.com/undercasetype/Fraunces) | [OFL 1.1](app/static/fonts/Fraunces-OFL.txt) |
| Inter | `Inter-normal.woff2` | Copyright 2016 The Inter Project Authors (github.com/rsms/inter) | [OFL 1.1](app/static/fonts/Inter-OFL.txt) |
| JetBrains Mono | `JetBrainsMono-normal.woff2` | Copyright 2020 The JetBrains Mono Project Authors (github.com/JetBrains/JetBrainsMono) | [OFL 1.1](app/static/fonts/JetBrainsMono-OFL.txt) |

Dojo serves the fonts as they were downloaded and does not change them.

## Python packages

Dojo does not include these packages in the repository. `pip` installs them from PyPI, in the versions
pinned in [`requirements.txt`](requirements.txt). They are listed here so you know what you install. Their own
dependencies come with their own licenses too.

| Package | License | Notes |
|---|---|---|
| [fastapi](https://pypi.org/project/fastapi/) | MIT | |
| [starlette](https://pypi.org/project/starlette/) | BSD-3-Clause | |
| [pydantic](https://pypi.org/project/pydantic/) | MIT | |
| [uvicorn](https://pypi.org/project/uvicorn/) | BSD-3-Clause | |
| [gunicorn](https://pypi.org/project/gunicorn/) | MIT | |
| [httpx](https://pypi.org/project/httpx/) | BSD-3-Clause | |
| [azure-identity](https://pypi.org/project/azure-identity/) | MIT | |
| [segno](https://pypi.org/project/segno/) | BSD-3-Clause | |
| [mssql-python](https://pypi.org/project/mssql-python/) | MIT | The wheels include bundled Windows DLLs, which come under their own upstream licence (see the package). |
| [pywebpush](https://pypi.org/project/pywebpush/) | MPL-2.0 | |
| [py-vapid](https://pypi.org/project/py-vapid/) | MPL-2.0 | |
| [http-ece](https://pypi.org/project/http-ece/) | MIT | |
| [av](https://pypi.org/project/av/) (PyAV) | BSD-3-Clause | The binary wheels include FFmpeg and other libraries under their own licenses (LGPL and others; see the package). |

The licenses come from each package's published metadata. When you update a version, check its license
again.

## Content from Microsoft Learn and GitHub Docs

Dojo reads public pages from [Microsoft Learn](https://learn.microsoft.com) and
[GitHub Docs](https://docs.github.com). It quotes short passages word for word, always with a link to the
page they come from, and writes its own lessons and questions from them.

- That content belongs to its publishers. It is used under their license terms; see the source pages'
  license terms (each page links to them).
- The exam package files (`app/packages`) contain skill statements quoted from the official study guides
  (see the next section) and, per skill, the title, address and publisher of each documentation page Dojo
  cites for it. A `license` field in a cited page's entry is Dojo's own note about that documentation page.
  It is not a license grant, it does not cover the study guide text, and the page's own terms are what
  count.
- Lessons, questions, audio and video that Dojo generates are stored on the operator's own data share, not
  in this repository.
- Dojo never contains or uses real exam questions.

## Exam study guides

The exam package files quote the skills measured from two Microsoft Learn study guides, word for word:

| File | Source page | Skill statements |
|---|---|---|
| [`app/packages/gh-300.json`](app/packages/gh-300.json) | [Study guide for Exam GH-300: GitHub Copilot](https://learn.microsoft.com/en-us/credentials/certifications/resources/study-guides/gh-300) | 41 |
| [`app/packages/dp-800.json`](app/packages/dp-800.json) | [Study Guide for Exam DP-800: Developing AI-Enabled Database Solutions](https://learn.microsoft.com/en-us/credentials/certifications/resources/study-guides/dp-800) | 73 |

These 114 skill statements, and the domain and group headings they sit under, are quoted from the
Microsoft Learn study guides. They are © Microsoft and used under the
[Microsoft Learn terms of use](https://learn.microsoft.com/en-us/legal/termsofuse). They are not covered by
Dojo's MIT License. Each file records the page address, the "skills measured as of" date and the date Dojo
retrieved it; the current study guide on Microsoft Learn is what counts.

## Azure services

Dojo calls Azure services (Azure AI Foundry models, Azure AI Speech, Azure SQL Database, Azure App
Service). They are not part of this repository. Your use of them is under your own agreement with
Microsoft.

## Trademarks

Microsoft, Azure, Microsoft Learn, GitHub and GitHub Copilot are trademarks of the Microsoft group of
companies. Other names may be trademarks of their owners. Dojo is not an official Microsoft product and is
not affiliated with or endorsed by the Microsoft or GitHub certification programs.
