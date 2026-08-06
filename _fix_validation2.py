import io

p = 'src/fpl_intelligence/availability/validation.py'
with io.open(p, encoding='utf-8') as f:
    content = f.read()

# Fix unindented comment line.
content = content.replace(
    '            continue\n\n# Determine the decision cutoff to compare against.\n        cutoff = None',
    '            continue\n\n        # Determine the decision cutoff to compare against.\n        cutoff = None'
)

with io.open(p, 'w', encoding='utf-8', newline='') as f:
    f.write(content)
print('comment fixed')
