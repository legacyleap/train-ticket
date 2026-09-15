package travel.service;

import edu.fudan.common.util.Response;
import org.springframework.http.HttpHeaders;

/**
 * Order count and revenue for a trip, shown on the trip detail page.
 */
public interface OrderSummaryService {
    Response getOrderSummary(String tripId, HttpHeaders headers);
}
