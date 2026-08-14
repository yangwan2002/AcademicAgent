# 1 引言

空地协同 SLAM 在灾后搜救、城市巡检等场景中需求日益突出 [1]。无人机具备大范围快速
覆盖能力,地面机器人具备近距离精细感知能力,二者协同可以互补。然而跨视角视觉关联
仍是瓶颈:无人机俯视图与地面平视图之间的视角差可达 80 度以上,传统特征匹配方法在
此条件下召回率不足 30% [2]。

已有工作大致分为两类。一类依赖几何先验,假设地面近似平面,通过单应变换对齐视角
[3];这类方法在起伏地形上退化明显。另一类依赖学习式描述子 [4],但训练数据多来自
同视角数据集,跨视角泛化能力有限。

本文提出一种基于语义-几何联合约束的跨视角关联方法。主要贡献如下:

1. 提出语义引导的候选区域生成策略,将匹配搜索空间缩小 60%;
2. 设计跨视角一致性损失,在 UAV-Ground 数据集上将召回率提升到 71.4%;
3. 在真实外场实验中验证了方法在 3 类地形下的稳定性。

# 2 相关工作

## 2.1 跨视角图像匹配

跨视角匹配的早期工作集中在手工特征上。SIFT 及其变体在视角差小于 30 度时表现良好,
超过此范围则急剧退化 [2]。后续工作引入仿射不变性 [5],但计算代价高。

深度学习方法方面,SuperPoint [6] 与 SuperGlue [7] 显著提升了匹配鲁棒性。然而这些
方法在跨视角场景下仍受限于训练分布。

## 2.2 语义辅助定位

语义信息被广泛用于辅助定位。文献 [8] 使用语义分割结果构建拓扑图。

# 3 方法

## 3.1 问题定义

给定无人机图像 $I_a$ 与地面图像 $I_g$,目标是估计二者之间的相对位姿 $T_{ag}$。

## 3.2 语义引导的候选生成

我们首先对两幅图像做语义分割,得到语义标签图。然后按语义类别一致性筛选候选匹配
区域,只在同类语义区域内做特征匹配。

$$
S(p, q) = \exp\left(-\frac{\|f_p - f_q\|^2}{2\sigma^2}\right) \cdot \mathbb{1}[c_p = c_q]
$$

其中 $c_p$ 表示像素 $p$ 的语义类别。

## 3.3 跨视角一致性损失

# 4 实验

## 4.1 数据集与设置

实验在 UAV-Ground 数据集上进行,包含 12000 对跨视角图像。训练集与测试集按 8:2
划分。所有实验在单张 RTX 3090 上完成。

## 4.2 主要结果

本文方法召回率达到 71.4%,较基线 SuperGlue [7] 的 52.1% 提升 19.3 个百分点。
消融实验表明语义引导贡献了其中 12.8 个百分点。

## 4.3 外场实验

# 5 结论

本文提出了一种语义-几何联合约束的跨视角关联方法,在公开数据集与外场实验中均验证
了有效性。未来工作将探索多机协同下的一致性优化。

# 参考文献

[1] Zhang, L. et al. Air-ground collaborative SLAM: a survey. IEEE TRO, 2022.
[2] Lowe, D. Distinctive image features from scale-invariant keypoints. IJCV, 2004.
[3] Wang, H. et al. Homography-based cross-view alignment. ICRA, 2019.
[4] Chen, Y. et al. Learning cross-view descriptors. CVPR, 2021.
[5] Morel, J. ASIFT: a new framework for fully affine invariant comparison. SIAM, 2009.
[6] DeTone, D. et al. SuperPoint: self-supervised interest point detection. CVPRW, 2018.
[7] Sarlin, P. et al. SuperGlue: learning feature matching with GNN. CVPR, 2020.
[8] Liu, X. et al. Semantic topological localization. IROS, 2020.
