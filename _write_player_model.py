lines = []
lines.append('import os')
lines.append('')
lines.append('from dataclasses import dataclass, field')
lines.append('from datetime import datetime')
lines.append('from typing import Any')
lines.append('')
code = chr(10).join(lines)
print(code[:200])
