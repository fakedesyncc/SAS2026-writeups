В задаче был Telegram stickerpack, созданный за несколько минут до публикации таска. Все стикеры в нём были .tgs, то есть gzip-сжатые Lottie-анимации. На первый взгляд это обычный набор анимированных стикеров, но один файл отличался от остальных структурой анимации.

Поиск нужного стикера
Есть два рабочих способа сузить область поиска.

Первый путь - recon. По названию stickerpack-а можно найти оригинальный набор на сайтах, индексирующих Telegram-стикеры. У одного из стикеров также виден handle оригинального автора. Если сравнить оригинальный pack с кастомным, то окажется, что ровно один стикер есть в challenge-версии, но отсутствует в оригинале.

Второй путь - анализ самих .tgs. После распаковки видно, что почти все стикеры используют похожий стиль анимации: сгруппированные слои, distortion-like transforms и обычные долгоживущие элементы. Подозрительный стикер устроен иначе: в нём много отдельных shape-layer-ов, которые живут ровно один кадр.

Я пошёл вторым путём.

gzip -dc cat.tgs > cat.json
Базовая структура:

import json

data = json.load(open("cat.json"))

print(data["w"], data["h"], data["ip"], data["op"], data["fr"])
print(len(data["layers"]))
Анимация имеет размер 512x512, идёт с 0 по 39 кадр и содержит аномально много слоёв.

Аномальные слои
В Lottie shape-layer имеет ty = 4. У подозрительных слоёв были такие признаки:

слой живёт ровно один кадр: op - ip == 1;
кадры начинаются с 5;
на каждый кадр приходится 3-4 таких слоя;
у слоёв практически нулевой scale, например 0.01, 0.03, 0.09;
часть слоёв специально сделана parent-ами друг для друга, чтобы сбивать при просмотре дерева.
Пример быстрого поиска:

import json
from collections import defaultdict

data = json.load(open("cat.json"))

by_frame = defaultdict(list)

for layer in data["layers"]:
    if layer.get("ty") != 4:
        continue

    ip = layer.get("ip")
    op = layer.get("op")

    if op - ip == 1:
        by_frame[int(ip)].append(layer)

for frame in sorted(by_frame):
    print(frame, [layer["ind"] for layer in by_frame[frame]])
На выходе получается группа кадров 5..31. Это уже похоже на флаг: один кадр - один символ.

Что именно спрятано
Каждая буква флага была порезана на 3-4 vector shape-а. Эти shape-и лежат в одном кадре, но у layer transform-а выставлен микроскопический scale, поэтому при обычном рендере они невидимы.

Важно: parent-chain здесь не является частью решения. Он нужен скорее как шум. Для чтения флага нужно смотреть сами vector paths, а не итоговую Lottie-трансформацию.

Внутри shape-а Lottie хранит cubic Bezier:

{
  "ty": "sh",
  "ks": {
    "k": {
      "c": true,
      "v": [...],
      "i": [...],
      "o": [...]
    }
  }
}
Где:

v - вершины;
i - входящие Bezier handles;
o - исходящие Bezier handles;
c - замкнут ли контур.
То есть можно просто собрать все ty = "sh" из однофреймовых слоёв, сгруппировать их по ip и отрисовать без layer scale.

Минимальный рендерер:

import gzip
import json
import math
from collections import defaultdict
from PIL import Image, ImageDraw

with gzip.open("cat.tgs", "rt", encoding="utf-8") as f:
    data = json.load(f)

def cubic(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u**3*p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0],
        u**3*p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1],
    )

def collect_paths(items):
    paths = []

    for item in items or []:
        if item.get("ty") == "gr":
            paths.extend(collect_paths(item.get("it")))
        elif item.get("ty") == "sh":
            k = item["ks"]["k"]
            v, ins, outs = k["v"], k["i"], k["o"]
            closed = k.get("c", False)

            pts = []
            n = len(v)
            segs = n if closed else n - 1

            for idx in range(segs):
                j = (idx + 1) % n
                p0 = v[idx]
                p1 = [v[idx][0] + outs[idx][0], v[idx][1] + outs[idx][1]]
                p2 = [v[j][0] + ins[j][0], v[j][1] + ins[j][1]]
                p3 = v[j]

                for step in range(20):
                    pts.append(cubic(p0, p1, p2, p3, step / 20))

            paths.append((pts, closed))

    return paths

frames = defaultdict(list)

for layer in data["layers"]:
    if layer.get("ty") == 4 and layer.get("op") - layer.get("ip") == 1:
        frame = int(layer["ip"])
        frames[frame].extend(collect_paths(layer.get("shapes")))

for frame, paths in sorted(frames.items()):
    pts = [p for path, _ in paths for p in path]
    if not pts:
        continue

    min_x = min(x for x, y in pts)
    max_x = max(x for x, y in pts)
    min_y = min(y for x, y in pts)
    max_y = max(y for x, y in pts)

    size = 256
    margin = 20
    scale = (size - margin * 2) / max(max_x - min_x, max_y - min_y)

    img = Image.new("L", (size, size), 255)
    draw = ImageDraw.Draw(img)

    cx = (min_x + max_x) / 2
    cy = (min_y + max_y) / 2

    for path, closed in paths:
        poly = [
            ((x - cx) * scale + size / 2, (y - cy) * scale + size / 2)
            for x, y in path
        ]

        if closed:
            draw.polygon(poly, fill=0)
        else:
            draw.line(poly, fill=0, width=2)

    img.save(f"frame_{frame:02d}.png")
После этого получаются отдельные картинки для кадров 05..31.

Сборка флага
Первые кадры сразу задают формат:

05 = S
06 = A
07 = S
08 = {
31 = }
Оставшиеся кадры - символы тела флага.

<img width="1980" height="786" alt="pic" src="https://github.com/user-attachments/assets/4fa9e5d2-921d-4430-b73e-c18d833475ba" />
