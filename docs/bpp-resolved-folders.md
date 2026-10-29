# BPP review folders

BPP Intelligence and Mailing Address open in Active. Resolving or dismissing a review refreshes both lists; RESOLVED and DISMISSED records appear only in their Resolved folder. Existing completed records are included automatically, with no data migration or deletion. Source evidence, review outcomes and notes remain on the original records.

Each screen has independent folder selection and resets its status filter when changing folders. Active allows New/In Review filters; Resolved allows Resolved/Dismissed filters. Other filters still apply. Switching folders clears the old list while loading and request sequence checks prevent a slower response from repainting a different folder. The mailing summary counts both folders for the current query filters.

`/api/admin/intelligence/queue` accepts folder `active`, `resolved`, or `all`. Omission retains `all` for existing consumers. Folder filtering runs server-side after source normalization and district authorization, including sales-tax closure workflows. The response includes summary counts before folder selection. Existing review writes and official CAD data are unchanged.

Validation: 68 intelligence, mailing and linked-account tests pass, including folder membership, retained history/counts and invalid-folder rejection. Inline JavaScript syntax checked with Node.
