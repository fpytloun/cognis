# Diagram assets

Architecture diagrams are stored in two forms:

- editable `.excalidraw` sources in `docs/assets/diagrams/`
- rendered `.svg` files in `docs/assets/images/`

To regenerate a rendered SVG:

```bash
docker run --rm --platform linux/amd64 \
  -v "$(pwd)/docs/assets/diagrams:/input" \
  -v "$(pwd)/docs/assets/images:/output" \
  -w /input \
  jonarc06/excalirender:latest \
  cognis-ecosystem-overview.excalidraw --format svg -o /output/cognis-ecosystem-overview.svg
```

Repeat the same command pattern for the other `.excalidraw` files whenever a diagram changes.
