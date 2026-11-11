# MTO·MTS 프로세스 시뮬레이션 장표

코일 10톤이 주문에서 출하까지 가는 길을 MTO와 MTS 각각 단계별로 따라가는 장표 22장이다. 단계마다 하는 일, 금액의 위치, SAP에 남는 기록을 한 장에 담았다.

- **내려받기**: [PDF (22쪽)](mto-mts-simulation.pdf)
- **같은 내용의 문서**: [상세 5. 프로세스 시뮬레이션](../mto-mts-05-process-simulation.md)
- 숫자는 [상세 2. 원가 숫자 예시](../mto-mts-02-costing-examples.md)와 같다. 금액 단위는 천원이다.
- 이 페이지의 이미지와 PDF는 장표 원본을 렌더링한 것이다. 원본은 [src](src) 폴더에 있다(`deck.json`이 순서, `slides/*.html`이 장마다 한 파일).
- 장표를 고치면 이미지와 PDF도 다시 만든다. Node.js와 Playwright(Chromium)가 필요하고, 글꼴(Noto Sans KR)을 내려받으므로 인터넷이 되어야 한다. 저장소 루트에서 실행한다.

```bash
node docs/slides/tools/export.js docs/slides/src docs/slides/img docs/slides/mto-mts-simulation.pdf
```

| 구간 | 장 |
|---|---|
| [시작](#시작) | 1~2 |
| [MTO: 주문을 받고 만든다](#mto-주문을-받고-만든다) | 3~9 |
| [MTS: 만들어 두고 판다](#mts-만들어-두고-판다) | 10~17 |
| [두 흐름이 만나는 지점](#두-흐름이-만나는-지점) | 18~20 |
| [정리](#정리) | 21~22 |

---

## 시작

**1. 표지**

![1. 표지](img/01-cover.png)

**2. 숫자와 등장하는 주문**

![2. 숫자와 등장하는 주문](img/02-setup.png)

---

## MTO: 주문을 받고 만든다

**3. MTO 전체 흐름**

![3. MTO 전체 흐름](img/03-mto-map.png)

**4. ① 수주**

![4. MTO ① 수주](img/04-mto-1.png)

**5. ② MO 생성**

![5. MTO ② MO 생성](img/05-mto-2.png)

**6. ③ 생산**

![6. MTO ③ 생산](img/06-mto-3.png)

**7. ④ 입고**

![7. MTO ④ 입고](img/07-mto-4.png)

**8. ⑤ 출하·청구**

![8. MTO ⑤ 출하·청구](img/08-mto-5.png)

**9. ⑥ 월말**

![9. MTO ⑥ 월말](img/09-mto-6.png)

---

## MTS: 만들어 두고 판다

**10. MTS 전체 흐름**

![10. MTS 전체 흐름](img/10-mts-map.png)

**11. ① 계획**

![11. MTS ① 계획](img/11-mts-1.png)

**12. ② MO 생성**

![12. MTS ② MO 생성](img/12-mts-2.png)

**13. ③ 생산**

![13. MTS ③ 생산](img/13-mts-3.png)

**14. ④ 입고**

![14. MTS ④ 입고](img/14-mts-4.png)

**15. ⑤ 수주**

![15. MTS ⑤ 수주](img/15-mts-5.png)

**16. ⑥ 매칭·출하·청구**

![16. MTS ⑥ 매칭·출하·청구](img/16-mts-6.png)

**17. ⑦ 월말**

![17. MTS ⑦ 월말](img/17-mts-7.png)

---

## 두 흐름이 만나는 지점

**18. 재고의 주인은 한 곳에서 갈린다**

![18. 두 흐름이 만나는 지점](img/18-pivot.png)

**19. 남은 일반재고 4톤을 MTO 주문에 충당**

![19. 남은 일반재고 4톤을 MTO 주문에 충당](img/19-cross-1.png)

**20. 주문 잔량 2톤을 여재로 전환**

![20. 주문 잔량 2톤을 여재로 전환](img/20-cross-2.png)

---

## 정리

**21. MTO와 MTS, 단계별 차이**

![21. MTO와 MTS, 단계별 차이](img/21-compare.png)

**22. 설계 전에 확인할 것**

![22. 설계 전에 확인할 것](img/22-checks.png)
