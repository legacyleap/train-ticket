package travel.service;

import edu.fudan.common.entity.Order;
import edu.fudan.common.util.Response;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Computes order statistics per trip inside travel-service so the UI needs a single call.
 */
@Service
public class OrderSummaryServiceImpl implements OrderSummaryService {

    @Autowired
    private RestTemplate restTemplate;

    private String getServiceUrl(String serviceName) {
        return "http://" + serviceName;
    }

    @Override
    public Response getOrderSummary(String tripId, HttpHeaders headers) {
        HttpEntity requestEntity = new HttpEntity(null, headers);
        String order_service_url = getServiceUrl("ts-order-service");
        ResponseEntity<Response<ArrayList<Order>>> re = restTemplate.exchange(
                order_service_url + "/api/v1/orderservice/order/query/" + tripId,
                HttpMethod.GET,
                requestEntity,
                new ParameterizedTypeReference<Response<ArrayList<Order>>>() {
                });
        List<Order> orders = re.getBody() == null || re.getBody().getData() == null ? new ArrayList<>() : re.getBody().getData();
        double revenue = 0;
        int paid = 0;
        for (Order order : orders) {
            if (order.getStatus() == 1) {
                paid++;
                revenue += Double.parseDouble(order.getPrice() == null ? "0" : order.getPrice());
            }
        }
        Map<String, Object> summary = new HashMap<>();
        summary.put("orders", orders.size());
        summary.put("paid", paid);
        summary.put("revenue", revenue);
        return new Response<>(1, "Success", summary);
    }
}
