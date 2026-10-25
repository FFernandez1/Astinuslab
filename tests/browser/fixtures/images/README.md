# Browser image fixtures

The image files in this directory are intentionally zero bytes. Playwright smoke tests point the isolated application at this directory so the UI can exercise filename discovery, operating-system classification, and image selection without downloading or committing VM image contents.

Keep fixture names representative of supported `.img` and `.qcow2` inputs. Do not replace them with real disk images. Tests that need image bytes must create disposable content under a temporary directory.
