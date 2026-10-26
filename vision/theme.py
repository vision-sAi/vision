"""The one accent colour, shared by rich (the transcript, the cards) and prompt_toolkit (the input
box, the pickers, the status bar). A hex rather than `bright_cyan`/`ansicyan`, which the two
libraries and the terminal's palette each rendered a slightly different blue."""

ACCENT = "#4fd1e0"  # a soft cyan: the `◆ Vision` mark, borders, the `●` reply gutter, picker cursors
