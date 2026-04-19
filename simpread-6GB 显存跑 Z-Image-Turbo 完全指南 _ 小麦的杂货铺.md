> 本文由 [简悦 SimpRead](http://ksria.com/simpread/) 转码， 原文地址 [bingowith.me](http://bingowith.me/2025/11/28/z-image-turbo-low-vram-setup/#Diffusion-Model-%E4%B9%9F%E7%94%A8%E9%87%8F%E5%8C%96%E7%89%88%E6%9C%AC)

> 手把手教你用 6GB 显存跑通阿里的 Z-Image-Turbo，拖拉机启动！

[](#前言 "前言")前言
--------------

我的拖拉机又开动了（指显卡风扇的噪音）。

这次是阿里开源的 Z-Image-Turbo，6GB 显存也能跑，而且效果还挺好。这篇博客就是纯纯的配置教程，不整那些虚的，直接告诉你怎么让小显存的卡也能愉快生图。

[](#准备工作 "准备工作")准备工作
--------------------

首先，你需要：

*   ComfyUI（应该已经装好了吧，注意需要更新到最新版本，显卡驱动也要最新）
*   6-12GB 显存（我用的 2060 6GB）
*   16 以上内存（我用的是 16GB 内存，其实已经 swap 了）
*   足够的硬盘空间下模型（量化版本所有东西最好保证 15GB 左右的空余空间）

[](#第一步：拿官方工作流 "第一步：拿官方工作流")第一步：拿官方工作流
--------------------------------------

官方已经给你做好工作流了，直接拖进 ComfyUI 的 web 界面就能用：

👉 **官方工作流**：[https://comfyanonymous.github.io/ComfyUI_examples/z_image/](https://comfyanonymous.github.io/ComfyUI_examples/z_image/)

拖进去之后你会发现缺模型，别慌，继续往下看。

[](#第二步：下载模型文件 "第二步：下载模型文件")第二步：下载模型文件
--------------------------------------

按照官方文档，你需要三个文件，这 3 个文件官方都给了下载地址，但是除了 VAE 之外，其余的要用量化版本：

```
Text encoder: qwen_3_4b.safetensors
→ 放在 ComfyUI/models/text_encoders/

Diffusion model: z_image_turbo_bf16.safetensors
→ 放在 ComfyUI/models/diffusion_models/

VAE: ae.safetensors (Flux 1 VAE)
→ 放在 ComfyUI/models/vae/
```

**重点来了**：

### [](#VAE-直接下原版 "VAE - 直接下原版")VAE - 直接下原版

VAE 不用量化，直接下 Flux 1 的 VAE 就行：

*   用官方提供的地址
*   扔进 `ComfyUI/models/vae/` 文件夹

### [](#Text-Encoder-用量化版本 "Text Encoder - 用量化版本")Text Encoder - 用量化版本

这里我们用 GGUF 量化的 Qwen3-4B，省显存神器：

**1. 下载模型**

去这里：[https://huggingface.co/unsloth/Qwen3-4B-GGUF/tree/main](https://huggingface.co/unsloth/Qwen3-4B-GGUF/tree/main)

我用的是 `Qwen3-4B-Q6_K.gguf`，6GB 显存完全够用。

如果你显存更小，可以试试 Q5 或 Q4 版本，但质量会稍微下降。

**2. 装自定义节点**

要用 GGUF 格式需要装个插件：

```
cd ComfyUI/custom_nodes
git clone https://github.com/city96/ComfyUI-GGUF
```

然后重启 ComfyUI。

如果是 windows 的 portable 版本，还需要回到 comfyui 解压的那个目录执行一下:

```
.\python_embeded\python.exe -s -m pip install -r .\ComfyUI\custom_nodes\ComfyUI-GGUF\requirements.txt
```

**3. 放文件**

把下载的 `.gguf` 文件放到 `ComfyUI/models/text_encoders/` 文件夹。

### [](#Diffusion-Model-也用量化版本 "Diffusion Model - 也用量化版本")Diffusion Model - 也用量化版本

主模型也要量化，不然显存爆炸：

**下载 FP8 量化版本**

去这里：[https://huggingface.co/T5B/Z-Image-Turbo-FP8/tree/main](https://huggingface.co/T5B/Z-Image-Turbo-FP8/tree/main)

下载 FP8 量化的版本, 当前有两个`z-image-turbo-fp8-e4m3fn.safetensors`和`z-image-turbo-fp8-e5m2.safetensors`，精度和广度的区别，用前者就可以。

**放文件**

扔进 `ComfyUI/models/diffusion_models/` 文件夹。

[](#第三步：工作流配置 "第三步：工作流配置")第三步：工作流配置
-----------------------------------

在 ComfyUI 里，你需要把节点指向量化后的模型：

1.  **CLIPLoader**：选择你下载的 GGUF 文件（这个需要更换节点, 使用 CLIPLoader(GGUF) 节点）
2.  **UNet 加载器**：选择 FP8 量化的模型
3.  **VAE 节点**：选择原版 Flux VAE  
    [![](http://bingowith.me/images/z-image-comfyui.png)](http://bingowith.me/images/z-image-comfyui.png)

### [](#Sampler-设置 "Sampler 设置")Sampler 设置

用最快的配置：

*   **Sampler**: Euler
*   **Scheduler**: Simple
*   **Steps**: 8（Z-Image-Turbo 只需要 8 步）

[](#实际表现 "实际表现")实际表现
--------------------

我的配置：

*   显卡：2060 6GB VRAM
*   内存: 16GB
*   配置：Euler + Simple，8 steps
*   速度：**大概 2 分钟一张图**

虽然不算特别快，但考虑到：

*   显存才 6GB
*   质量确实不错
*   不会爆显存
*   支持中文文字渲染

这速度我是很满意的。

[](#常见问题 "常见问题")常见问题
--------------------

### [](#Q-显存还是不够怎么办？ "Q: 显存还是不够怎么办？")Q: 显存还是不够怎么办？

A: 试试更低的量化版本：

*   Text Encoder 换成 Q4_K 或 Q5_K
*   如果还不够，关掉其他占显存的程序

### [](#Q-速度太慢了怎么办？ "Q: 速度太慢了怎么办？")Q: 速度太慢了怎么办？

A:

*   确保用了 Euler + Simple
*   Steps 别设太高，8 步就够
*   分辨率调低

### [](#Q-生成的图质量不好？ "Q: 生成的图质量不好？")Q: 生成的图质量不好？

A:

*   检查是不是量化版本用太低了（比如 Q2）
*   确保 VAE 用的是原版
*   prompt 要写详细点

[](#结语 "结语")结语
--------------

好了，配置就这么多。现在你应该可以听到拖拉机的轰鸣声了。

感谢阿里开源，感谢做量化版本的大佬们，让我们这些小显存用户也能玩上 AI 生图。

祝生图愉快！

* * *

[](#快速链接 "快速链接")快速链接
--------------------

*   官方工作流：[https://comfyanonymous.github.io/ComfyUI_examples/z_image/](https://comfyanonymous.github.io/ComfyUI_examples/z_image/)
*   Qwen3-4B GGUF：[https://huggingface.co/unsloth/Qwen3-4B-GGUF](https://huggingface.co/unsloth/Qwen3-4B-GGUF)
*   Z-Image-Turbo FP8：[https://huggingface.co/T5B/Z-Image-Turbo-FP8](https://huggingface.co/T5B/Z-Image-Turbo-FP8)
*   ComfyUI-GGUF 插件：[https://github.com/city96/ComfyUI-GGUF](https://github.com/city96/ComfyUI-GGUF)
*   Z-Image 官方仓库：[https://github.com/Tongyi-MAI/Z-Image](https://github.com/Tongyi-MAI/Z-Image)

[**前一篇**

深入理解 SSR/CSR 与 SPA/MPA：现代前端渲染模式详解

](http://bingowith.me/2025/12/09/understanding-ssr-csr-spa-mpa/)[**后一篇**

自行部署 Next.js Docker 的挑战：臃肿、平台绑定与隐藏的 “坑”

](http://bingowith.me/2025/11/16/nextjs-docker-deploy-pitfalls/)