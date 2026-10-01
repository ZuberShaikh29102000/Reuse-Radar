# arXiv LaTeX fixtures

Real LaTeX sources of three ATLAS papers, recorded with `scripts/record_arxiv_fixtures.py` from
`export.arxiv.org` and used only as test inputs for the filter stage.

| arXiv | Paper type |
|---|---|
| [2112.11876](https://arxiv.org/abs/2112.11876) | Search setting upper limits (HH → bbγγ), 2021 |
| [2401.05299](https://arxiv.org/abs/2401.05299) | Inclusive and differential cross-section measurement (ttW), 2024 |
| [2403.02793](https://arxiv.org/abs/2403.02793) | Differential measurement designed for reinterpretation (HEPData, Rivet), 2024 |

The ATLAS author-list files (`atlas_authlist.tex`) are left out on purpose. They follow the
bibliography, where the filter never reads, and contain thousands of personal names. Rights to
each paper are as stated on its arXiv abstract page.
