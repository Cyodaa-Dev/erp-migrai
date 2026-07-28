"""把註解與字串常值遮罩成空白字元的掃描器。

供 `blocks.py` 的 `split_blocks` 使用：所有關鍵字／結構比對（`DATABASE`、
區塊起始、`END <KIND>`、內層嚴格 header、整行是否為註解）一律不是對原始行
文字比對，而是對這裡產生的「遮罩後」副本比對——把屬於註解或字串常值的
字元換成空白，但保留完全相同的行數與換行字元，每一行的長度也不變。這樣
「程式碼裡剛好長得像關鍵字的註解或字串內容」就不會誤觸任何結構判斷。
`Block.lines`、`snippet()` 用的仍然是原始（未遮罩）文字，使用者看到的
永遠是真正的原始碼。

這一層與 `blocks.py` 一樣刻意**不使用文法**，也刻意**不會失敗**——單一一趟
逐字元掃描，絕不拋出例外，且保證每一輪都嚴格前進，不會卡在同一個位置。

遮罩範圍：`{ ... }` 區塊註解（可跨行、不支援巢狀）、`--` 與 `#` 行註解（到行尾
為止）、雙引號與單引號字串常值。字串常值遮罩時保留頭尾的引號字元本身、只把
中間內容換成空白——這是刻意的：`GLOBALS "common.4gl"` 這種 include 形式需要
靠引號的存在才能被判斷為字串常值，若連引號都遮罩掉就會讓 include 形式失去
可辨識性。註解（`{ }`／`--`／`#`）則是連同起始符號一起整段遮罩，這樣一整行
都是註解時，遮罩後會變成純空白，能沿用既有的「空白行」判斷，不需要額外的
註解專屬分支。

未終止的復原規則：字串常值在找到結尾引號之前先遇到換行，視為未終止；`{`
區塊註解在全文找不到對應 `}` 時，視為未終止——這兩種都只在「開啟的那一行」
結尾復原（換行字元本身留給呼叫端當一般字元處理），並各自附上一筆
`UnsupportedSyntax`（reason 提到「未終止」），把受影響範圍限制在那一行，
不會吃掉後面整份檔案。這與 `ddl.py` 的 `split_statements` 採用同樣的復原
原則，但兩者是各自獨立的實作，不共用程式碼。

遮罩掃描器不理解的部分：不支援字串常值裡的跳脫字元或雙寫引號（例如 SQL 的
`''`，但一行寫得規規矩矩的 `''` 逃逸引號數量必為偶數，天真配對仍能完整涵蓋
整段字串常值，不會產生假的「未終止」回報）；`{ ... }` 不支援巢狀（遇到第一個
`}` 就視為註解結束）；除了 `\n`／`\r` 之外的其他 Unicode 換行字元不被特殊
處理。這些都不是真實 T100 程式常見的寫法，若遇到，請開 parser-gap-report
issue。

已知限制（`{ ... }` 忘記寫對應 `}` 時，回報而非縮小遮罩範圍）：`{ ... }`
一律配對「全文下一個」`}`——這是 4GL 編譯器本身的行為，我們的讀法沒有錯，
錯的是原始碼；若程式作者忘了寫對應的 `}`，遮罩會一路配對到後面某個不相干、
但恰好正確關閉的 `{ ... }` 為止，把中間所有真正的程式碼（可能橫跨好幾個
`FUNCTION`／`MAIN`）都吞掉。這裡刻意**不猜測** `}` 「應該」出現在哪裡去縮小
遮罩範圍——那等於憑空捏造原始碼裡不存在的結構。

判斷依據不是猜測門檻，而是 4GL 本身的語法事實：`{ ... }` 不支援巢狀，因此
一段正確關閉、內容完全合理的註解，其內容裡絕不可能再出現 `{`。所以規則是：
當遮罩掉的 `{ ... }` 橫跨超過一行，且內容裡（開啟的那個 `{` 之後）又出現
另一個 `{`，代表這段內容幾乎不可能是合法的單一註解——幾乎可以肯定是忘了寫
對應的 `}`，導致外層的 `{` 一路配對到後面某個不相干的 `}` 為止。這裡回報
一筆 `UnsupportedSyntax`（`line` 指向外層 `{` 開啟的那一行，reason 附上內層
`{` 出現的行號），讓這個吃掉整段內容的後果變得可見、可被人工檢查，而不是
悄悄消失又不聲張。

這個規則只在橫跨超過一行時套用：單行的 `{ ... }`，就算內容裡剛好也有 `{`
（例如刻意示範大括號寫法的註解 `{ 使用 { 大括號 } 表示 }`），也不會觸發——
單行沒有「跨行吞掉不相干內容」的風險。這種單行示範大括號的註解，若真的
恰好因為前後脈絡而横跨多行，觸發這個規則會是一次可以理解、可被接受的
誤報：因為那樣的原始碼本身就是真正語意含糊（註解確實在第一個 `}` 就結束），
回報出來讓人工複核是合理的。

已知殘留限制（刻意接受，不縮小遮罩範圍去猜測）：如果忘了寫的 `}` 吞掉的
內容剛好完全不含任何 `{`（例如吞掉的只是幾行不相干的敘述句或程式碼），
這個規則就無法分辨它與一段合法、正常結束的說明性註解有什麼不同——因此
**不會**被回報。這是刻意的取捨：猜測 `}` 「應該」出現在哪裡去縮小遮罩範圍，
等於憑空捏造原始碼裡不存在的結構，比不報更糟。這個殘留限制留給後面階段
（statement 層的 parser）去承擔後果——當它讀到這段被吞掉又冒出來的殘留文字
時，會因為語法不合法而自然地回報出來，只是那時候的錯誤訊息會指向殘留文字
本身，而不是這裡的遮罩掃描器。

雙寫引號（`''`）逃逸慣例：這個掃描器不理解 SQL 常見的 `''` 雙寫引號逃逸寫法
（例如 `'It''s ok'`）。之所以安全，是因為一行寫得規規矩矩的 `''` 逃逸，引號
數量必為偶數；即使天真地逐一配對「開引號／關引號」而不理解逃逸語意，整段
字串常值仍然會被完整配對到最後一個引號為止，不會產生假的「未終止」回報，
只是中間某個本來語意上是逃逸引號的字元被當成一對獨立的開／關引號看待
（反正兩者都只是把內容換成空白，遮罩後的結果沒有差異）。未來若要接手這段
程式碼，不需要重新推導這個結論。
"""

from __future__ import annotations

import re

from erp_migrai.inventory.models import UnsupportedSyntax, snippet

# 換行判定：\r\n 視為一次、單獨的 \n 或單獨的 \r（後面不是 \n）也各自視為一次。
# 用來讓 { ... } 相關的行號計算與回報，與 text.splitlines() 認定的行數一致，
# 不論來源檔案用哪一種換行風格。
_LINE_BREAK = re.compile(r"\r\n|\r(?!\n)|\n")


def mask_comments_and_strings(
    text: str, *, source_file: str
) -> tuple[str, list[UnsupportedSyntax]]:
    """回傳 (遮罩後的文字, 未終止構件的 UnsupportedSyntax 清單)。

    遮罩後的文字與原始文字行數相同、每行長度相同、換行字元（`\\n`／`\\r`）完全不動；
    差別只在於原本屬於註解或字串常值的字元被換成空白字元。呼叫端拿這份遮罩後的文字
    做關鍵字／結構比對，藉此避免註解或字串裡剛好長得像關鍵字的文字誤觸判斷。

    掃描規則（逐字元、單一趟、絕不拋例外、保證每一輪都嚴格前進 `i`）：
    - `{ ... }`：區塊註解，可跨行，不支援巢狀（一律配對「下一個」`}`）。起始符號、
      內容、結尾符號整段換成空白。找不到對應 `}` 視為未終止，只在開啟那一行的行尾
      復原（不會吃到後面幾行），並附上一筆 `UnsupportedSyntax`。若找到的 `}` 距離
      夠遠、橫跨超過一行，且內容裡（開啟的那個 `{` 之後）又出現另一個 `{`——
      `{ ... }` 不支援巢狀，因此這是忘了寫對應 `}` 的健全訊號，不是猜測——則附上
      一筆 `UnsupportedSyntax`（`line` 指向外層 `{` 開啟的那一行，reason 附上內層
      `{` 出現的行號）。這裡**刻意不縮小遮罩範圍**——`{ ... }` 配對「下一個」`}`
      是 4GL 編譯器本身的行為，我們的讀法沒有錯、錯的是原始碼；擅自猜測 `}`
      「應該」出現在哪裡等於憑空捏造原始碼裡不存在的結構。因此只把這個後果
      **回報**出來，讓損失可見、可被人工檢查，而不是悄悄吞掉又不聲張。
    - `--`、`#`：行註解，到行尾為止，整段（含起始符號）換成空白。
    - `"..."`、`'...'`：字串常值。只有中間內容換成空白，頭尾引號本身保留——這是刻意
      設計：`GLOBALS "file.4gl"` 這種 include 形式要靠引號存在才能被判斷為字串常值，
      連引號都遮罩掉會讓這個判斷失去依據。在找到對應的結尾引號之前先遇到換行，視為
      未終止，只在該行行尾復原，並附上一筆 `UnsupportedSyntax`。
    - 註解裡的引號不是字串、字串裡的註解符號不是註解：兩種掃描各自只找自己的
      結束符號，不會互相影響。

    雙寫引號（`''`）逃逸慣例：這個掃描器不理解 SQL 常見的 `''` 雙寫引號逃逸寫法
    （例如 `'It''s ok'`）。之所以安全，是因為一行寫得規規矩矩的 `''` 逃逸，引號
    數量必為偶數；即使天真地逐一配對「開引號／關引號」而不理解逃逸語意，整段
    字串常值仍然會被完整配對到最後一個引號為止，不會產生假的「未終止」回報，
    只是中間某個本來語意上是逃逸引號的字元被當成一對獨立的開／關引號看待
    （反正兩者都只是把內容換成空白，遮罩後的結果沒有差異）。未來若要接手這段
    程式碼，不需要重新推導這個結論。
    """
    n = len(text)
    chars = list(text)
    unsupported: list[UnsupportedSyntax] = []
    i = 0
    line = 1

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if chars[k] not in ("\n", "\r"):
                chars[k] = " "

    while i < n:
        ch = text[i]

        if ch == "{":
            open_line = line
            start = i
            end = text.find("}", i + 1)
            if end != -1:
                span_breaks = len(_LINE_BREAK.findall(text, start, end + 1))
                if span_breaks >= 1:
                    inner_pos = text.find("{", start + 1, end)
                    if inner_pos != -1:
                        inner_line = open_line + len(_LINE_BREAK.findall(text, start, inner_pos))
                        unsupported.append(
                            UnsupportedSyntax(
                                file=source_file,
                                line=open_line,
                                snippet=snippet(text[start : end + 1]),
                                reason=(
                                    "{ 區塊註解涵蓋的範圍裡，在第 "
                                    + str(inner_line)
                                    + " 行又出現了一個 {，但 { } 註解不支援巢狀，"
                                    "很可能是忘了寫對應的 }，導致這段 { ... } "
                                    "一路配對到後面不相關的 } 為止"
                                ),
                            )
                        )
                blank(start, end + 1)
                line += span_breaks
                i = end + 1
                continue
            newline_at = text.find("\n", i + 1)
            recover = newline_at if newline_at != -1 else n
            blank(start, recover)
            unsupported.append(
                UnsupportedSyntax(
                    file=source_file,
                    line=open_line,
                    snippet=snippet(text[start:recover]),
                    reason="{ 區塊註解未終止（缺少對應的 }），已在行尾恢復掃描",
                )
            )
            i = recover
            continue

        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            newline_at = text.find("\n", i + 2)
            recover = newline_at if newline_at != -1 else n
            blank(i, recover)
            i = recover
            continue

        if ch == "#":
            newline_at = text.find("\n", i + 1)
            recover = newline_at if newline_at != -1 else n
            blank(i, recover)
            i = recover
            continue

        if ch == '"' or ch == "'":
            quote = ch
            open_line = line
            start = i
            j = i + 1
            closed = False
            while j < n:
                c2 = text[j]
                if c2 == quote:
                    closed = True
                    break
                if c2 == "\n":
                    break
                j += 1
            if closed:
                blank(start + 1, j)
                i = j + 1
                continue
            blank(start + 1, j)
            unsupported.append(
                UnsupportedSyntax(
                    file=source_file,
                    line=open_line,
                    snippet=snippet(text[start:j]),
                    reason=f"字串常值未終止（缺少結尾 {quote}），已在行尾恢復掃描",
                )
            )
            i = j
            continue

        if ch == "\n":
            line += 1
        elif ch == "\r" and (i + 1 >= n or text[i + 1] != "\n"):
            # 單獨的 \r（Mac 傳統換行、不是 \r\n 的一部分）也是一次換行，
            # 否則 \r-only 來源檔案的行號計數會完全卡在第 1 行不動。
            line += 1
        i += 1

    return "".join(chars), unsupported
