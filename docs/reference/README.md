# Reference

How EMT works today, one area a page. Each describes the code on `dev`; when a page and the code
disagree, the code is right and the page is fixed in the same PR as the change.

| Page | Covers |
|---|---|
| [architecture.md](architecture.md) | The modules and layers, reading and writing the database, the job coordinator, the tables, the checks |
| [pricing-and-settlement.md](pricing-and-settlement.md) | Pricing a half-hour, the band, the 6-hour cap, what "settled" means, the asker, the heatmap's states, the re-fetch |
| [data-management-and-devices.md](data-management-and-devices.md) | The Data Management page and its tools, backups, the support file; how devices are split, rebuilt and checked |
| [v4-import-and-first-run.md](v4-import-and-first-run.md) | The welcome page, the two-step restore, the gate, the import |
| [api.md](api.md) | The pages, `/api/energy`, the route families, tariff agreements and prices |

Why things are as they are — the decisions, with their dates and the alternatives — is in
[../history/](../history/).
