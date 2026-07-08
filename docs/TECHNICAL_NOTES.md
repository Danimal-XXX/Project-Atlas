### Future Refactor

Current implementation places application code inside the `knowledge_base` directory.

As Atlas evolves into a general-purpose knowledge platform, application code will be moved to the repository root.

Proposed future structure:

Project Atlas/
    config/
    crawler/
    scripts/
    tests/
    sources/
    output/
    cache/

The current structure is retained until the HelpScout migration is complete to minimise risk.
