# mes-to-be-doc

MES 재구축 To-Be 문서

## MTO·MTS 병행 운영

SAP ERP를 S/4HANA로 전환하고 자체 MES를 재구축하면서, 주문생산(MTO)과 계획생산(MTS)을 함께 운영하기 위한 분석 문서다.

| 문서 | 내용 | 주 독자 |
|---|---|---|
| [본문: MTO·MTS 병행 운영 분석서](docs/mto-mts-hybrid-analysis.md) | 결론, 전체 구조, 의사결정 체크리스트 | 전원 |
| [상세 1. 업무 시나리오](docs/mto-mts-01-scenarios.md) | 계획생산 유형별로 누가 무엇을 하는지 | 영업, 생산관리 |
| [상세 2. 원가 숫자 예시](docs/mto-mts-02-costing-examples.md) | MTO, MTS, 재고 전환의 분개와 금액 | 원가, 회계 |
| [상세 3. SAP 설정과 전환](docs/mto-mts-03-sap-config-conversion.md) | 필요한 설정, S/4HANA 전환 영향, 도입 순서 | SAP 담당, PMO |
| [상세 4. MES 설계 요건](docs/mto-mts-04-mes-requirements.md) | MES 데이터 구조, 매칭 로직, SAP 인터페이스 | MES 설계자 |
| [상세 5. 프로세스 시뮬레이션](docs/mto-mts-05-process-simulation.md) | 코일 10톤을 MTO와 MTS로 각각 따라가 보기 | 전원 |
| [상세 6. 철강 업계 관행과 세분화 자재코드](docs/mto-mts-06-steel-practice.md) | 규격이 다른 재고의 충당 규칙, 재고 구분, 미리 만들 지점, 재고 지표 | 생산관리, 기준정보 |
| [시뮬레이션 장표](docs/slides/README.md) | 상세 5를 장표 22장으로 만든 것. 이미지로 바로 보거나 [PDF](docs/slides/mto-mts-simulation.pdf)로 내려받는다 | 전원 |

처음 읽는다면 본문의 0장(한 장 요약)과 시뮬레이션 장표부터 보기를 권한다.

### 읽을 때 알아둘 점

- **근거 표기**: [확인]은 공개 자료로 확인한 내용이다. [검증]은 SAP 표준 지식에 근거한 설명이라 프로토타입 시스템에서 확인해야 한다.
- **조사의 한계**: 조사 환경에서 SAP 사이트 원문을 직접 열 수 없어 검색 결과의 요약과 인용문으로 확인했다. 이동유형, 필드명, 설정 값은 대상 시스템에서 다시 확인한다. 자세한 내용은 본문 부록 B에 있다.
- **결정 번호**: D1~D24는 모든 문서에서 같은 결정을 가리킨다. 목록은 본문 10장과 상세 3 14장에 있다.
