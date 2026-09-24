import Document, { Head, Html, Main, NextScript } from "next/document";

export default class CustomDocument extends Document {
  render() {
    return <Html lang="zh-CN"><Head><link rel="icon" href="/favicon.svg" type="image/svg+xml" /></Head><body><Main /><NextScript /></body></Html>;
  }
}
