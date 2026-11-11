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

처음 읽는다면 본문의 0장(한 장 요약)과 상세 5(시뮬레이션)부터 보기를 권한다.
