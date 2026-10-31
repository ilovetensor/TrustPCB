"""The print stylesheet of the report, one rule per line. Pasted into the document by render_html."""

CSS = r'''
:root {
    color-scheme: light;
}
* {
    box-sizing: border-box;
}
html {
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
}
body {
    font: 15px/1.5 -apple-system,"Segoe UI",Inter,Helvetica,Arial,sans-serif;
    color: #1c2024;
    background: #fff;
    margin: 0;
    font-variant-numeric: tabular-nums;
}
main {
    max-width: 920px;
    margin: 0 auto;
    padding: 32px 24px 64px;
}
h1 {
    font-size: 28px;
    margin: 0 0 4px;
}
h2 {
    font-size: 20px;
    margin: 36px 0 10px;
}
h3 {
    font-size: 16px;
    margin: 18px 0 4px;
}
p {
    margin: 6px 0;
}
.meta,.muted {
    color: #5b6570;
}
.mono,td.n,code {
    font-family: inherit;
    font-variant-numeric: tabular-nums;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 8px 0;
}
th,td {
    text-align: left;
    padding: 5px 8px;
    vertical-align: top;
}
table:not(.metrics) tr:nth-child(even) td {
    background: rgba(28,32,36,.045);
}
th {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .04em;
    color: #5b6570;
    background: rgba(28,32,36,.07);
}
.pill {
    display: inline-block;
    padding: 1px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    background: #eceff2;
    color: #2c3238;
}
.sev-critical {
    background: #ffd9e0;
    color: #8a0f2b;
}
.sev-high {
    background: #ffe0e0;
    color: #8a1c1c;
}
.sev-medium {
    background: #ffeccf;
    color: #7a4a00;
}
.sev-low {
    background: #fff4c2;
    color: #665500;
}
.sev-info {
    background: #dcecfb;
    color: #0c4a8a;
}
.cls-measured {
    background: #dcf1e6;
    color: #145a3b;
}
.cls-simulated {
    background: #dcedf7;
    color: #0e5a78;
}
.cls-opinion {
    background: #ebe5f7;
    color: #46308a;
}
.cls-not_tested {
    background: #e8eaed;
    color: #444;
}
.card {
    background: #f5f6f8;
    border-radius: 12px;
    padding: 14px 16px;
    margin: 14px 0;
    break-inside: avoid;
}
.card h3 {
    margin-top: 0;
}
.notice {
    background: #fff3df;
    border-radius: 8px;
    padding: 8px 12px;
    margin: 12px 0;
}
.changes {
    background: #e6f3fa;
    border-radius: 8px;
    padding: 8px 12px;
    margin: 12px 0;
}
svg.board,img.still {
    width: 100%;
    height: auto;
    border-radius: 8px;
    background: #fff;
}
img.still {
    display: block;
    max-width: 100%;
}
.counts span {
    margin-right: 8px;
}
td.n,table.vc th {
    white-space: nowrap;
}
ul {
    padding-left: 20px;
    margin: 4px 0;
}
code {
    font-size: 13px;
    font-weight: 600;
}
.small {
    font-size: 13px;
}
.power {
    margin: 12px 0;
}
.power>div {
    display: grid;
    grid-template-columns: minmax(0,190px) 1fr 58px;
    gap: 10px;
    align-items: center;
    margin: 6px 0;
    font-size: 13px;
}
.power i {
    position: relative;
    display: block;
    height: 14px;
    background: #eef0f2;
    box-shadow: inset 0 0 0 1px #c8ced5;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
}
.power i::after {
    content: "";
    position: absolute;
    top: -2px;
    bottom: -2px;
    left: 0;
    right: 0;
    pointer-events: none;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
    background: linear-gradient(#7d8590,#7d8590) 25% 0/1px 100% no-repeat,linear-gradient(#7d8590,#7d8590) 50% 0/1px 100% no-repeat,linear-gradient(#7d8590,#7d8590) 75% 0/1px 100% no-repeat;
}
.power b {
    position: relative;
    display: block;
    height: 100%;
    background: #1c2024;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
}
.power b.before {
    background: repeating-linear-gradient(135deg,#3b424a 0 1.5px,transparent 1.5px 4.5px);
}
.power b.now::after {
    content: "";
    position: absolute;
    right: -1px;
    top: -3px;
    bottom: -3px;
    width: 2px;
    background: #ff4a1c;
    print-color-adjust: exact;
    -webkit-print-color-adjust: exact;
}
.power strong {
    text-align: right;
}
@media print {
    body {
        font-size: 14px;
        line-height: 1.45;
    }
    main {
        padding: 0;
    }
    th,td {
        padding: 4px 8px;
    }
    h2 {
        margin-top: 28px;
    }
    h2,h3,.card h3+p,.card p:has(+ul) {
        break-after: avoid;
    }
    h2,h3 {
        break-inside: avoid;
    }
    .card {
        break-inside: auto;
    }
    .card.keep,svg.board,img,tr,li,.power,.notice,.changes {
        break-inside: avoid;
    }
    svg.board {
        max-height: 120mm;
    }
    svg.board[width] {
        display: block;
        width: auto;
        max-width: 100%;
        margin: 0 auto;
    }
    table.vc td:first-child {
        width: 36%;
    }
    table.metrics,table.metrics tbody {
        display: block;
    }
    table.metrics tbody {
        display: grid;
        grid-template-columns: 1fr auto 1fr auto;
    }
    table.metrics tr {
        display: contents;
    }
    table.metrics tr:nth-child(even) td:first-child {
        padding-left: 28px;
    }
    table.metrics td.n {
        text-align: right;
    }
}'''
